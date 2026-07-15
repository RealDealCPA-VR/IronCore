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
