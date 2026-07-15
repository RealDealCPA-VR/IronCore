"""Deterministic patch appliers for the edit-format ladder (SPEC §4.3, MODELS §3).

RULES
-----
- Pure text transforms: each applier is ``apply(original_text, edit) ->
  PatchResult``. NOTHING in this module touches the filesystem — the
  write-side tools (fs_write.py) own disk I/O and path jailing.
- Stdlib only (difflib powers the closest-match hints).
- Failures are MECHANICAL and explained: ``PatchResult.reason`` is written
  to be fed straight back to the model by the repair loop (SPEC §5.4,
  IC-503) — e.g. "SEARCH block 1 not found; closest match at line 42: …" —
  never a stack trace or a shrug.
- Never guess: an ambiguous SEARCH block or an unlocatable hunk fails
  loudly instead of picking a candidate.
- Line endings are preserved: the original's DOMINANT newline (CRLF vs LF)
  is detected and restored in ``new_text`` (mixed-ending files come out
  uniformly in their dominant ending); a missing final newline stays
  missing. Edits arriving with the "wrong" newline flavor still apply —
  both sides are compared LF-normalized.
- Forgiving parsing for small models (MODELS.md §3): unified-diff hunks
  tolerate ±HUNK_FUZZ_LINES of start-line drift, prose before/after the
  diff body, blank context lines missing their leading space, and sloppy
  hunk-header counts; SEARCH/REPLACE markers tolerate varying widths.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

#: apply_unified_diff tolerates a hunk's start line being off by this many lines.
HUNK_FUZZ_LINES = 3
#: apply_whole_file rejects replacement content larger than this many characters.
WHOLE_FILE_MAX_CHARS = 1_000_000

_HUNK_RE = re.compile(r"^@@\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@")
_SEARCH_MARK_RE = re.compile(r"^<{4,}\s*SEARCH\s*$")
_DIVIDER_MARK_RE = re.compile(r"^={4,}\s*$")
_REPLACE_MARK_RE = re.compile(r"^>{4,}\s*REPLACE\s*$")
_HUNK_OP_CHARS = (" ", "+", "-", "\\")


@dataclass
class PatchResult:
    """Outcome of one pure patch application.

    ``reason`` (on failure) is the mechanical explanation, formatted for the
    repair loop to feed back to the model. ``no_op`` (on success) means the
    edit leaves the text identical, so the tool can report "no changes".
    """

    ok: bool
    new_text: str | None = None
    reason: str | None = None
    no_op: bool = False


# --- shared helpers ----------------------------------------------------------


def _detect_newline(text: str) -> str:
    """The text's dominant line ending. Ties and no-newline default to LF."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _restore_newline(text_lf: str, newline: str) -> str:
    """Convert LF-normalized text back to the detected dominant ending."""
    return text_lf if newline == "\n" else text_lf.replace("\n", newline)


def _closest_hint(lines: list[str], want: str) -> str:
    """A repair-loop hint: where is the line most similar to ``want``?"""
    if not lines:
        return "the file is empty"
    matches = difflib.get_close_matches(want, lines, n=1, cutoff=0.5)
    if not matches:
        return "no similar line found in the file"
    return f"closest match at line {lines.index(matches[0]) + 1}: {matches[0]!r}"


# --- unified diff ------------------------------------------------------------


@dataclass
class _Hunk:
    header: str
    old_start: int
    old_len: int
    ops: list[tuple[str, str]] = field(default_factory=list)
    #: a "\ No newline at end of file" marker followed a kept (+ or context) line.
    no_newline_new: bool = False


def _parse_unified_diff(diff: str) -> tuple[list[_Hunk], str | None]:
    """Extract hunks. Prose before/after the diff body is tolerated; content
    that would silently drop a later hunk is an error, never a guess."""
    raw = diff.replace("\r\n", "\n").split("\n")
    if raw and raw[-1] == "":
        raw.pop()  # artifact of a trailing newline, not an op line
    n = len(raw)
    hunks: list[_Hunk] = []
    cur: _Hunk | None = None
    for i, line in enumerate(raw):
        m = _HUNK_RE.match(line)
        if m:
            cur = _Hunk(
                header=line.strip(),
                old_start=int(m.group(1)),
                old_len=int(m.group(2)) if m.group(2) is not None else 1,
            )
            hunks.append(cur)
            continue
        if cur is None:
            continue  # preamble (---/+++/index/prose) before the first hunk
        if line == "":
            # Models often drop the leading space on blank context lines. Treat
            # the blank as context only if the diff body clearly continues.
            j = i + 1
            while j < n and raw[j] == "":
                j += 1
            if j < n and (raw[j][0] in _HUNK_OP_CHARS or _HUNK_RE.match(raw[j])):
                cur.ops.append((" ", ""))
                continue
            break  # trailing blank(s): the diff body ended here
        if line.startswith("diff --git") or (
            line.startswith("--- ") and i + 1 < n and raw[i + 1].startswith("+++ ")
        ):
            return [], (
                "the diff contains more than one file section; "
                "edit_file applies a diff to ONE file"
            )
        tag = line[0]
        if tag in (" ", "+", "-"):
            cur.ops.append((tag, line[1:]))
        elif tag == "\\":
            prev = cur.ops[-1][0] if cur.ops else " "
            if prev in ("+", " "):
                cur.no_newline_new = True
        else:
            # Non-op content: tolerate trailing prose, but never silently drop a hunk.
            if any(_HUNK_RE.match(rest) for rest in raw[i + 1 :]):
                return [], (
                    f"unrecognized line inside the diff at line {i + 1}: {line!r}; "
                    "every hunk line must start with ' ', '+', '-' or '\\'"
                )
            break
    return hunks, None


def _segment_matches(lines: list[str], pos: int, want: list[str]) -> bool:
    """Exact line match, with a trailing-whitespace-insensitive fallback."""
    for k, want_line in enumerate(want):
        have = lines[pos + k]
        if have != want_line and have.rstrip() != want_line.rstrip():
            return False
    return True


def _locate(lines: list[str], want: list[str], expected: int) -> int | None:
    """Find ``want`` at ``expected``, fuzzing outward up to ±HUNK_FUZZ_LINES.

    Nearest position wins (0, +1, -1, +2, -2, …) so small drifts never jump
    to a farther lookalike.
    """
    limit = len(lines) - len(want)
    for step in range(2 * HUNK_FUZZ_LINES + 1):
        delta = (step + 1) // 2 if step % 2 else -(step // 2)  # 0, +1, -1, +2, -2, …
        pos = expected + delta
        if 0 <= pos <= limit and _segment_matches(lines, pos, want):
            return pos
    return None


def apply_unified_diff(original: str, diff: str) -> PatchResult:
    """Apply a unified diff with fuzzy line anchoring (±HUNK_FUZZ_LINES)."""
    hunks, parse_err = _parse_unified_diff(diff)
    if parse_err is not None:
        return PatchResult(ok=False, reason=parse_err)
    if not hunks:
        return PatchResult(
            ok=False,
            reason=(
                "no @@ hunks found in the diff; emit standard unified-diff hunks "
                "(@@ -start,count +start,count @@ followed by ' '/'-'/'+' lines)"
            ),
        )

    newline = _detect_newline(original)
    norm = original.replace("\r\n", "\n")
    lines = norm.splitlines()
    # An empty original that gains lines gets a final newline unless a marker says otherwise.
    out_trailing = norm.endswith("\n") or norm == ""

    shift = 0  # current-lines index minus old-file line index, from applied hunks
    for idx, hunk in enumerate(hunks, 1):
        old_block = [text for op, text in hunk.ops if op in (" ", "-")]
        if not old_block:
            # Pure insertion: nothing to anchor on. "-N,0" means insert AFTER old line N.
            at = hunk.old_start + shift if hunk.old_len == 0 else hunk.old_start - 1 + shift
            at = min(max(at, 0), len(lines))
            added = [text for op, text in hunk.ops if op == "+"]
            lines[at:at] = added
            shift += len(added)
        else:
            expected = hunk.old_start - 1 + shift
            pos = _locate(lines, old_block, expected)
            if pos is None:
                first = next((t for t in old_block if t.strip()), old_block[0])
                return PatchResult(
                    ok=False,
                    reason=(
                        f"hunk {idx} ({hunk.header}) does not apply: its context was not "
                        f"found within ±{HUNK_FUZZ_LINES} lines of line {max(expected, 0) + 1}; "
                        f"expected {first!r}; {_closest_hint(lines, first)}. "
                        "Regenerate the hunk from the current file contents."
                    ),
                )
            # Keep the FILE's context lines (the fuzzy match tolerates trailing-
            # whitespace drift); take only '+' lines from the diff.
            new_segment: list[str] = []
            file_i = pos
            for op, text in hunk.ops:
                if op == " ":
                    new_segment.append(lines[file_i])
                    file_i += 1
                elif op == "-":
                    file_i += 1
                else:
                    new_segment.append(text)
            lines[pos : pos + len(old_block)] = new_segment
            shift += (pos - expected) + (len(new_segment) - len(old_block))
        if hunk.no_newline_new:
            out_trailing = False

    result_lf = "\n".join(lines)
    if out_trailing and lines:
        result_lf += "\n"
    new_text = _restore_newline(result_lf, newline)
    return PatchResult(ok=True, new_text=new_text, no_op=(new_text == original))


# --- search / replace --------------------------------------------------------


def parse_search_replace(text: str) -> tuple[list[tuple[str, str]], str | None]:
    """Parse Aider-style marker text into (search, replace) pairs.

    Tolerates prose around the blocks and marker widths of 4+ characters.
    Returns ``(blocks, None)`` or ``([], reason)`` on malformed input.
    """
    blocks: list[tuple[str, str]] = []
    state = "outside"
    search: list[str] = []
    replace: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if state == "outside":
            if _SEARCH_MARK_RE.match(line):
                state, search, replace = "search", [], []
        elif state == "search":
            if _DIVIDER_MARK_RE.match(line):
                state = "replace"
            elif _REPLACE_MARK_RE.match(line):
                return [], (
                    "malformed SEARCH/REPLACE block: '>>>>>>> REPLACE' arrived before "
                    "the '=======' divider"
                )
            else:
                search.append(line)
        else:  # replace
            if _REPLACE_MARK_RE.match(line):
                blocks.append(("\n".join(search), "\n".join(replace)))
                state = "outside"
            else:
                replace.append(line)
    if state != "outside":
        missing = "'======='" if state == "search" else "'>>>>>>> REPLACE'"
        return [], f"unterminated SEARCH/REPLACE block: missing the {missing} marker"
    if not blocks:
        return [], (
            "no SEARCH/REPLACE blocks found; use:\n<<<<<<< SEARCH\n(exact existing lines)\n"
            "=======\n(replacement lines)\n>>>>>>> REPLACE"
        )
    return blocks, None


def _occurrence_lines(text: str, needle: str) -> list[int]:
    """1-based line numbers of each (non-overlapping) occurrence of ``needle``."""
    out: list[int] = []
    start = 0
    while (idx := text.find(needle, start)) != -1:
        out.append(text.count("\n", 0, idx) + 1)
        start = idx + len(needle)
    return out


def apply_search_replace(
    original: str, blocks: str | list[tuple[str, str]]
) -> PatchResult:
    """Apply SEARCH/REPLACE blocks; each SEARCH must match EXACTLY ONCE.

    ``blocks`` is either already-parsed (search, replace) pairs or raw marker
    text (see :func:`parse_search_replace`). Blocks apply in order, each
    against the text produced by the previous one.
    """
    if isinstance(blocks, str):
        parsed, parse_err = parse_search_replace(blocks)
        if parse_err is not None:
            return PatchResult(ok=False, reason=parse_err)
        pairs = parsed
    else:
        pairs = [(s, r) for s, r in blocks]
    if not pairs:
        return PatchResult(ok=False, reason="no SEARCH/REPLACE blocks given")

    newline = _detect_newline(original)
    text = original.replace("\r\n", "\n")
    for i, (raw_search, raw_replace) in enumerate(pairs, 1):
        search = raw_search.replace("\r\n", "\n")
        replace = raw_replace.replace("\r\n", "\n")
        if search == "":
            return PatchResult(
                ok=False,
                reason=(
                    f"SEARCH block {i} is empty; copy the exact lines to change "
                    "from the current file into the SEARCH section"
                ),
            )
        count = text.count(search)
        if count == 0:
            first = next((ln for ln in search.split("\n") if ln.strip()), search)
            return PatchResult(
                ok=False,
                reason=(
                    f"SEARCH block {i} not found in the file; "
                    f"{_closest_hint(text.splitlines(), first)}. The SEARCH text must "
                    "match the current file exactly, including whitespace."
                ),
            )
        if count > 1:
            where = ", ".join(str(n) for n in _occurrence_lines(text, search))
            return PatchResult(
                ok=False,
                reason=(
                    f"SEARCH block {i} is ambiguous: it matches {count} times "
                    f"(at lines {where}). Never guessing — add surrounding lines "
                    "to the SEARCH text so it matches exactly once."
                ),
            )
        text = text.replace(search, replace, 1)

    new_text = _restore_newline(text, newline)
    return PatchResult(ok=True, new_text=new_text, no_op=(new_text == original))


# --- whole file ---------------------------------------------------------------


def apply_whole_file(original: str, new_content: str) -> PatchResult:
    """Replace the entire text, with a size guard and no-op detection."""
    if len(new_content) > WHOLE_FILE_MAX_CHARS:
        return PatchResult(
            ok=False,
            reason=(
                f"whole_file content is {len(new_content):,} characters, over the "
                f"{WHOLE_FILE_MAX_CHARS:,}-character cap — refusing (this looks like a "
                "runaway generation; use search_replace or unified_diff instead)"
            ),
        )
    newline = _detect_newline(original)
    new_text = _restore_newline(new_content.replace("\r\n", "\n"), newline)
    return PatchResult(ok=True, new_text=new_text, no_op=(new_text == original))
