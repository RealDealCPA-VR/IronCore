"""Diff viewer: a unified diff / edit payload rendered with +/- coloring.

Used in two places (SPEC §3.1: "transcript ... diff views" + the approval modal
"shows the full diff"):

* ``ApprovalScreen`` renders a write/edit request's preview through ``DiffView``
  so the exact effect is shown as colored added/removed lines, not a wall of
  monochrome text.
* ``ToolCard`` (transcript.py) colorizes an ``edit_file`` call's diff payload
  inline on the tool card.

Two surfaces, one pure core:

* ``diff_to_text(payload)`` — the pure, testable transform: a ``rich.text.Text``
  with ``+added`` green, ``-removed`` red, ``@@`` hunk headers cyan, SEARCH/
  REPLACE markers yellow (with the search body red and the replace body green),
  everything else dim context. The text is ``no_wrap`` so wide lines SCROLL
  rather than wrap-break (the container owns the horizontal scrollbar).
* ``looks_like_diff(payload)`` — a cheap shape check used to decide whether a
  non-write preview should route through the diff renderer or fall back to
  plain text (a shell ``$ …`` line, a URL, a bare ``write_file`` header do not).

Rendering only — this widget holds no engine reference and mounts nothing from
``core/`` (docs/ARCHITECTURE.md §4). Model/tool text is only ever wrapped in
``Text`` (never interpreted as Rich console markup).
"""

from __future__ import annotations

import re

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.widgets import Static

#: A unified-diff hunk header, e.g. ``@@ -1,3 +1,4 @@``.
_HUNK_RE = re.compile(r"^@@\s*-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s*@@")
#: Aider-style SEARCH/REPLACE markers (widths of 4+ tolerated, like patch.py).
_SEARCH_RE = re.compile(r"^<{4,}\s*SEARCH\s*$")
_DIVIDER_RE = re.compile(r"^={4,}\s*$")
_REPLACE_RE = re.compile(r"^>{4,}\s*REPLACE\s*$")

# Rich style strings, keyed to the semantic line class. Kept as strings so the
# resulting Text.spans carry a directly-assertable style (tests read them back).
_STYLE_ADDED = "green"
_STYLE_REMOVED = "red"
_STYLE_HUNK = "bold cyan"
_STYLE_FILE = "bold"
_STYLE_MARKER = "bold yellow"
_STYLE_CONTEXT = "dim"


def looks_like_diff(payload: str) -> bool:
    """True when ``payload`` carries diff shape worth coloring.

    Recognizes unified-diff hunk headers, ``+``/``-`` prefixed content lines,
    and SEARCH/REPLACE marker blocks. A shell command line (``$ …``), a URL, or
    a plain ``write_file path (N bytes)`` header does not qualify — those fall
    back to plain text in the approval modal.
    """
    if not payload:
        return False
    for raw in payload.split("\n"):
        line = raw.rstrip("\r")
        if _HUNK_RE.match(line) or _SEARCH_RE.match(line) or _REPLACE_RE.match(line):
            return True
        head = line[:1]
        if head in ("+", "-") and not line.startswith(("+++ ", "--- ")):
            return True
    return False


def search_replace_to_unified(payload: str) -> str | None:
    """Rewrite an aider ``SEARCH``/``REPLACE`` block as ``-old`` / ``+new`` lines.

    Returns ``None`` when the payload does not parse as one (a unified diff, a
    prose blob) so the caller can fall back to rendering it verbatim. The result
    is a plain diff-shaped string that :func:`diff_to_text` colours as
    removed/added — the same exact effect the offline demo narrates, without the
    ``<<<<<<<`` / ``=======`` / ``>>>>>>>`` conflict markers competing with the
    change itself for the eye at the one moment a human is deciding.

    The ``tools.patch`` parser is imported locally: this module stays a rendering
    leaf whose import graph never reaches into the engine (docs/ARCHITECTURE.md
    §4), and ``parse_search_replace`` is a pure, side-effect-free transform.
    """
    from ironcore.tools.patch import parse_search_replace

    blocks, error = parse_search_replace(payload)
    if error is not None or not blocks:
        return None
    lines: list[str] = []
    for search, replace in blocks:
        lines += [f"- {ln}" for ln in search.splitlines()]
        lines += [f"+ {ln}" for ln in replace.splitlines()]
    return "\n".join(lines)


def clean_edit_payload(payload: str) -> str:
    """A diff/edit payload made presentable: SEARCH/REPLACE blocks become
    ``-``/``+`` lines, everything else is returned untouched. Idempotent and
    total — a payload that is already a unified diff (or unparseable) passes
    straight through, so the caller can always hand the result to
    :func:`diff_to_text`."""
    return search_replace_to_unified(payload) or payload


def _classify(line: str, region: str | None) -> tuple[str, str | None]:
    """Style for one line + the next SEARCH/REPLACE region state.

    ``region`` threads the SEARCH/REPLACE state machine so a marker block's body
    (which has no ``+``/``-`` prefix) still colors as removed/added text.
    """
    if _SEARCH_RE.match(line):
        return _STYLE_MARKER, "search"
    if region == "search" and _DIVIDER_RE.match(line):
        return _STYLE_MARKER, "replace"
    if _REPLACE_RE.match(line):
        return _STYLE_MARKER, None
    if region == "search":
        return _STYLE_REMOVED, "search"
    if region == "replace":
        return _STYLE_ADDED, "replace"
    # Unified-diff / plain classification.
    if line.startswith(("+++ ", "--- ")):
        return _STYLE_FILE, None
    if _HUNK_RE.match(line):
        return _STYLE_HUNK, None
    if line.startswith("+"):
        return _STYLE_ADDED, None
    if line.startswith("-"):
        return _STYLE_REMOVED, None
    return _STYLE_CONTEXT, None


def diff_to_text(payload: str, *, max_lines: int | None = None) -> Text:
    """Render a diff / edit payload as styled, non-wrapping ``Text``.

    ``max_lines`` caps the output (transcript cards stay compact); a truncated
    tail gets a dim ``… (+N more line(s))`` note. ``no_wrap=True`` means wide
    lines are never broken — the surrounding container scrolls horizontally.
    Never raises: any string (even one with no diff shape) renders as context.
    """
    text = Text(no_wrap=True)
    lines = payload.split("\n")
    dropped = 0
    if max_lines is not None and len(lines) > max_lines:
        dropped = len(lines) - max_lines
        lines = lines[:max_lines]
    region: str | None = None
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        style, region = _classify(line, region)
        text.append(line, style=style)
    if dropped:
        text.append(f"\n… (+{dropped} more line(s))", style="dim italic")
    return text


class DiffView(ScrollableContainer):
    """A colored diff in a horizontally + vertically scrollable box.

    The inner ``Static`` sizes to the diff's widest (non-wrapping) line, so when
    the diff is wider than the box the container shows a horizontal scrollbar
    instead of breaking lines. Height is content-driven up to a cap, then the
    vertical scrollbar takes over.
    """

    DEFAULT_CSS = """
    DiffView {
        height: auto;
        max-height: 20;
        overflow-x: auto;
        overflow-y: auto;
    }
    DiffView > #diff-body {
        width: auto;
        height: auto;
    }
    """

    def __init__(self, payload: str, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._payload = payload
        self._text = diff_to_text(payload)

    def compose(self) -> ComposeResult:
        yield Static(self._text, id="diff-body")

    def diff_text(self) -> Text:
        """The rendered diff as ``Text`` — the read surface for tests."""
        return self._text
