"""/skill (PKG-4): list skills and inject one's instructions into the next turn.

    /skill                — LIST discoverable skills + one-line descriptions
    /skill <name>         — inject <name>'s full body for the next turn. A PROJECT
                            skill (clone-borne) shows a confirmation summary the
                            first time instead (does NOT inject).
    /skill run <name>     — CONFIRM a project skill + inject it now.

A skill is a ``<dir>/SKILL.md`` file (``docs/SKILLS.md``); discovery + the
open-standard parse live in ``ironcore/skills.py``. This command is the USER
path; the model has its own lazy-body path via the ``use_skill`` tool. Both
share one per-workspace confirmation registry (``skills.is_confirmed`` /
``mark_confirmed``), so approving a project skill here also unblocks
``use_skill`` for the model.

FIRST-USE CONFIRMATION (SAFETY T8, mirroring ``commands/workflowcmd.py``). A
project skill arrives with ``git clone``; the first ``/skill <name>`` returns a
summary and asks the user to confirm with ``/skill run <name>``. User-home
skills are trusted (like ``~/.ironcore/IRONCORE.md``) and inject immediately.

Injection is via the app hook ``inject_context`` (the app owns the
``engine._conversation`` seam — CONTRACTS §6: a command mutates only its context
/ calls app hooks, never engine internals). With no live app (headless / tests)
the body is returned inline so it is never lost. Every ``ctx.extra`` key is
optional; a missing dependency degrades to a readable message.
"""

from __future__ import annotations

from pathlib import Path

from ironcore.commands._helpers import resolve_workspace
from ironcore.commands.base import CommandContext, SlashCommand
from ironcore.skills import (
    LoadedSkills,
    Skill,
    discover_skills,
    is_confirmed,
    mark_confirmed,
)

#: Header framing an injected skill body as trusted, harness-authored context.
_INJECT_HEADER = "# Loaded skill: {name}\n\n"

#: First token that is a sub-command, not a skill name.
_RESERVED = ("run",)


def _cmd_skill(ctx: CommandContext, args: str) -> str:
    args = args.strip()
    skills_cfg = getattr(ctx.settings, "skills", None)
    if skills_cfg is not None and not skills_cfg.enabled:
        return "Skills are disabled ([skills] enabled = false in your config)."
    workspace = resolve_workspace(ctx)
    user_home = ctx.extra.get("user_home")  # test/headless seam; default Path.home()
    scan_root = workspace if workspace is not None else Path.cwd()
    loaded = discover_skills(scan_root, ctx.settings, user_home=user_home)

    if not args:
        return _list(loaded, workspace)
    first, _, rest = args.partition(" ")
    if first.lower() == "run":
        name = rest.strip()
        if not name:
            return "Usage: /skill run <name>"
        return _load(ctx, loaded, workspace, name, confirm=True)
    # Non-"run": the whole arg string is the skill name (names may contain spaces).
    return _load(ctx, loaded, workspace, args, confirm=False)


def _list(loaded: LoadedSkills, workspace: Path | None) -> str:
    """List every discoverable skill with a one-line description and source tag."""
    if not loaded.skills:
        lines = [
            "No skills found. Add one at .ironcore/skills/<name>/SKILL.md "
            "(authoring guide: docs/SKILLS.md)."
        ]
        if loaded.skipped:
            lines.append(f"({len(loaded.skipped)} SKILL.md file(s) skipped — see below)")
            lines.extend(f"  [skipped] {s.path} — {s.reason}" for s in loaded.skipped)
        return "\n".join(lines)
    lines = ["Available skills:"]
    for skill in loaded.skills:
        tag = ""
        if skill.source == "project" and not is_confirmed(workspace, skill.name):
            tag = "  [project — approve with /skill run <name>]"
        lines.append(f"  {skill.name} — {skill.description or '(no description)'}{tag}")
    lines.append("Load one into the next turn with /skill <name>.")
    if loaded.skipped:
        lines.append(f"({len(loaded.skipped)} SKILL.md file(s) skipped — malformed frontmatter)")
    return "\n".join(lines)


def _load(
    ctx: CommandContext,
    loaded: LoadedSkills,
    workspace: Path | None,
    name: str,
    *,
    confirm: bool,
) -> str:
    """Confirm-or-inject a single named skill."""
    skill = loaded.find(name)
    if skill is None:
        if name.lower() in _RESERVED:  # e.g. a bare "/skill run" with no name
            return f"Usage: /skill {name.lower()} <name>"
        return f"No skill named {name!r}. Run /skill to list available skills."

    if skill.source == "project":
        if not confirm and not is_confirmed(workspace, skill.name):
            return _confirmation_summary(skill)
        mark_confirmed(workspace, skill.name)  # confirmed now (or previously)

    if _inject(ctx, skill):
        return f"Loaded skill {skill.name!r} into context — it will guide the next turn."
    # No live app to inject into (headless / a test without an app): hand back the
    # body so it is not silently lost.
    return f"Skill {skill.name!r} (no live session to inject into):\n\n{skill.body}"


def _confirmation_summary(skill: Skill) -> str:
    """First-use summary for a clone-borne project skill (SAFETY T8). No inject."""
    return "\n".join(
        [
            f"Skill {skill.name!r} (project skill from this repository): "
            f"{skill.description or '(no description)'}",
            f"  Source: {skill.path}",
            "  Its instructions are injected into the model's context on use. They cannot "
            "bypass the safety gate — any script the skill references still runs through "
            "run_command under the EXEC gate, deny-list and workspace jail.",
            f"First use in this workspace — approve to load it:  /skill run {skill.name}",
        ]
    )


def _inject(ctx: CommandContext, skill: Skill) -> bool:
    """Place the skill body into the running conversation via the app hook.

    Returns True on success. The app owns the ``engine._conversation`` seam
    (CONTRACTS §6); with no app hook wired we return False and the caller falls
    back to returning the body inline.
    """
    app = ctx.extra.get("app")
    if app is None or not hasattr(app, "inject_context"):
        return False
    text = _INJECT_HEADER.format(name=skill.name) + skill.body
    try:
        app.inject_context(text)
    except Exception:  # noqa: BLE001 — an app-hook failure must not crash the command
        return False
    return True


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        "skill",
        "load a skill's instructions",
        "/skill [<name>] | run <name>",
        _cmd_skill,
    ),
)
