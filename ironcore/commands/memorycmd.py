"""/memory (IC-807): view or append to IRONCORE.md project memory (SPEC §11.1).

``/memory``               — show the whole IRONCORE.md (capped).
``/memory <section>``     — show one ``## <section>`` (e.g. ``/memory build``).
``/memory show <section>``— same, explicit verb.
``/memory add <text>``    — append ``text`` as a bullet inside the user-owned
                            section (the ``<!-- IRONCORE:USER ... -->`` sentinels
                            ``/init`` writes and preserves).

All synchronous filesystem work; the IRONCORE.md format helpers live in
``initcmd`` (its owner). ``add`` never touches the generated sections, so a
subsequent ``/init`` refresh keeps every user note.
"""

from __future__ import annotations

from pathlib import Path

from ironcore.commands._helpers import resolve_workspace
from ironcore.commands.base import CommandContext, SlashCommand
from ironcore.commands.initcmd import (
    IRONCORE_MD,
    extract_user_section,
    set_user_section,
)

#: Cap on a viewed blob so a large file never floods the transcript.
_VIEW_CAP = 6000


def _cap(text: str, limit: int = _VIEW_CAP) -> str:
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return text[:limit] + f"\n… [truncated: {dropped} more chars — open {IRONCORE_MD}]"


def _sections(text: str) -> list[tuple[str, str]]:
    """Split into ``(heading-name, body-including-heading)`` for each ``## X``."""
    out: list[tuple[str, str]] = []
    name: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if name is not None:
                out.append((name, "\n".join(buf).strip("\n")))
            name = line[3:].strip()
            buf = [line]
        elif name is not None:
            buf.append(line)
    if name is not None:
        out.append((name, "\n".join(buf).strip("\n")))
    return out


def _find_section(text: str, target: str) -> str | None:
    target_l = target.strip().lower()
    for name, body in _sections(text):
        if name.lower() == target_l or name.lower().startswith(target_l):
            return body
    if target_l in ("user", "notes", "user notes"):
        body = extract_user_section(text)
        if body is not None:
            return f"## User notes\n{body}"
    return None


def _cmd_memory(ctx: CommandContext, args: str) -> str:
    ws = resolve_workspace(ctx)
    if ws is None:
        ws = Path.cwd()
    md_path = ws / IRONCORE_MD
    args = args.strip()
    verb, _, rest = args.partition(" ")
    verb_l = verb.lower()

    if verb_l == "add":
        return _add(md_path, rest.strip())

    if not md_path.is_file():
        return f"No {IRONCORE_MD} yet — run /init to create it."
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Could not read {IRONCORE_MD}: {exc}"

    if not args:
        return _cap(text)

    target = rest.strip() if verb_l in ("show", "view") else args
    if not target:
        return _cap(text)
    section = _find_section(text, target)
    if section is None:
        names = ", ".join(name for name, _ in _sections(text)) or "none"
        return f"No section matching {target!r} in {IRONCORE_MD}. Sections: {names}"
    return _cap(section)


def _add(md_path: Path, text: str) -> str:
    if not text:
        return "Usage: /memory add <text>"
    if not md_path.is_file():
        return f"No {IRONCORE_MD} yet — run /init first, then /memory add."
    try:
        current = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Could not read {IRONCORE_MD}: {exc}"
    body = extract_user_section(current)
    bullet = f"- {text}"
    if body is None:
        new_body = bullet
    else:
        # Drop the default placeholder hint the first time a real note is added.
        keep = body if body and not body.startswith("_Your notes live here.") else ""
        new_body = f"{keep}\n{bullet}".strip("\n")
    updated = set_user_section(current, new_body)
    try:
        md_path.write_text(updated, encoding="utf-8", newline="\n")
    except OSError as exc:
        return f"Could not write {IRONCORE_MD}: {exc}"
    return f"Added to {IRONCORE_MD} User notes: {text}"


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        "memory",
        "view or edit project memory",
        "/memory [section] | /memory add <text>",
        _cmd_memory,
    ),
)
