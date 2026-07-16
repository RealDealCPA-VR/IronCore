"""Handoff blocks: the machine half of docs/PROTOCOLS.md #2.

A handoff is written whenever a session ends, compacts, or an agent
finishes a task. The format is markdown with HTML-comment sentinels so it
is pleasant for humans and parseable for machines. HANDOFF.md is
append-only; the newest block is the pickup point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

BEGIN = "<!-- HANDOFF v1 BEGIN -->"
END = "<!-- HANDOFF v1 END -->"

_FIELDS = ("Context", "Changed", "Verified", "Next", "Gotchas")


@dataclass
class Handoff:
    author: str
    context: str  # what was being worked on, and why
    changed: str  # what actually changed (files, behavior)
    verified: str  # commands run + observed results ("not verified" is legal, but say it)
    next_steps: str  # the single most useful next action, then the rest
    gotchas: str = "none"
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def render(self) -> str:
        return "\n".join(
            [
                BEGIN,
                f"## Handoff — {self.timestamp} — {self.author}",
                f"**Context:** {self.context}",
                f"**Changed:** {self.changed}",
                f"**Verified:** {self.verified}",
                f"**Next:** {self.next_steps}",
                f"**Gotchas:** {self.gotchas}",
                END,
                "",
            ]
        )


def append_handoff(path: Path, handoff: Handoff) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(handoff.render() + "\n")


def read_handoffs(path: Path) -> list[Handoff]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    blocks = re.findall(re.escape(BEGIN) + r"(.*?)" + re.escape(END), text, flags=re.DOTALL)
    out: list[Handoff] = []
    for block in blocks:
        header_re = r"^## Handoff — (?P<ts>\S+) — (?P<author>.+)$"
        header = re.search(header_re, block, flags=re.MULTILINE)
        fields: dict[str, str] = {}
        for name in _FIELDS:
            m = re.search(rf"^\*\*{name}:\*\* (?P<v>.*)$", block, flags=re.MULTILINE)
            fields[name] = m.group("v").strip() if m else ""
        out.append(
            Handoff(
                author=header.group("author").strip() if header else "unknown",
                timestamp=header.group("ts") if header else "",
                context=fields["Context"],
                changed=fields["Changed"],
                verified=fields["Verified"],
                next_steps=fields["Next"],
                gotchas=fields["Gotchas"] or "none",
            )
        )
    return out


def latest_handoff(path: Path) -> Handoff | None:
    handoffs = read_handoffs(path)
    return handoffs[-1] if handoffs else None


# -- constructing a Handoff from a compaction/summary blob (SPEC §11.3) --------

#: The five section labels a compaction summary uses (core/compact.py), lowered.
_SUMMARY_LABELS: tuple[str, ...] = ("context", "changed", "verified", "next", "gotchas")

#: Summary label -> Handoff field name (the only label that renames is "next").
_LABEL_TO_FIELD = {
    "context": "context",
    "changed": "changed",
    "verified": "verified",
    "next": "next_steps",
    "gotchas": "gotchas",
}


def _flatten(text: str) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces so a parsed
    section stays on ONE line — render/read_handoffs roundtrip is line-based, and
    an embedded newline would truncate the field on read-back."""
    return " ".join(text.split())


def _label_of(line: str) -> tuple[str | None, str]:
    """If ``line`` opens one of the five summary sections, return
    ``(lowercase-label, text-after-the-label)``; else ``(None, "")``.

    Tolerates a leading/closing ``**`` bold marker and any case, so both a bare
    ``Context: ...`` (as compact.py emits) and a rendered ``**Context:** ...``
    header are recognized.
    """
    core = line.strip()
    bold = core.startswith("**")
    if bold:
        core = core[2:]
    lowered = core.lower()
    for label in _SUMMARY_LABELS:
        if lowered.startswith(f"{label}:"):
            rest = core[len(label) + 1:].strip()
            if bold and rest.startswith("**"):
                rest = rest[2:].strip()
            return label, rest
    return None, ""


def _split_summary(text: str) -> dict[str, str]:
    """Map Handoff-field-name -> flattened section body for every summary section
    present in ``text``. Lines before the first section (e.g. the
    ``# Compacted history …`` provenance header) are ignored. Returns ``{}`` when
    the text has no recognizable sections — the signal for a free-form blob.
    """
    chunks: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        label, rest = _label_of(line)
        if label is not None:
            current = label
            chunks.setdefault(label, [])
            if rest:
                chunks[current].append(rest)
        elif current is not None:
            chunks[current].append(line)
    return {_LABEL_TO_FIELD[label]: _flatten("\n".join(body)) for label, body in chunks.items()}


def handoff_from_summary(
    author: str, summary_text: str, *, next_steps: str = "", gotchas: str = "none"
) -> Handoff:
    """Build a :class:`Handoff` from a compaction/summary blob (SPEC §11.3).

    A compaction summary (``core/compact.py``) already carries the five handoff
    sections — Context / Changed / Verified / Next / Gotchas — so parse them into
    the matching fields. A free-form blob (no recognizable sections) is wrapped
    whole into ``context``. ``next_steps`` and ``gotchas`` are FALLBACKS, used only
    when the summary supplies no Next / Gotchas section of its own.

    Pure: no I/O and no branching on the clock (the only impurity is the
    :class:`Handoff` default timestamp it inherits).
    """
    fields = _split_summary(summary_text)
    if not fields:
        return Handoff(
            author=author,
            context=_flatten(summary_text),
            changed="",
            verified="",
            next_steps=next_steps,
            gotchas=gotchas,
        )
    return Handoff(
        author=author,
        context=fields.get("context", ""),
        changed=fields.get("changed", ""),
        verified=fields.get("verified", ""),
        next_steps=fields.get("next_steps") or next_steps,
        gotchas=fields.get("gotchas") or gotchas,
    )
