"""/goal (IC-803): the flagship objective + harness-enforced stop-condition.

``/goal <objective>`` stores the objective on both ``ctx.goal`` and
``engine.state.goal`` so the composer anchors it into every turn (SPEC §3.4).
The extra sub-commands make "done" a *verified* state, not a *claim*:

    /goal                 | /goal show   — report the goal + attached checks
    /goal <objective>     — set the objective (anchored every turn)
    /goal verify: <cmd>   — attach a verify command to the stop-condition
    /goal check           — run the attached verify commands and report met/unmet
    /goal clear           — clear the goal and its checks

``/goal check`` runs the attached commands through
``ironcore.core.verify.CommandVerifier`` and is therefore async — it returns an
ack immediately and posts the result via ``schedule``. Attached verify commands
are held in a module-level map keyed by workspace, because the TUI rebuilds a
fresh ``CommandContext`` per dispatch (``ctx`` cannot carry them across calls).
"""

from __future__ import annotations

from ironcore.commands._helpers import resolve_workspace
from ironcore.commands.base import CommandContext, SlashCommand
from ironcore.core.verify import CommandVerifier

#: workspace-key -> attached verify commands, durable across ephemeral contexts.
_VERIFY_CHECKS: dict[str, list[str]] = {}

_VERIFY_PREFIX = "verify:"


def _key(ctx: CommandContext) -> str:
    ws = resolve_workspace(ctx)
    return str(ws) if ws is not None else "<no-workspace>"


def _engine_state(ctx: CommandContext) -> object | None:
    engine = ctx.extra.get("engine")
    return getattr(engine, "state", None) if engine is not None else None


def _current_goal(ctx: CommandContext) -> str | None:
    if ctx.goal:
        return ctx.goal
    state = _engine_state(ctx)
    return getattr(state, "goal", None) if state is not None else None


def _cmd_goal(ctx: CommandContext, args: str) -> str:
    args = args.strip()
    if args in ("", "show"):
        return _show(ctx)
    if args == "clear":
        ctx.goal = None
        state = _engine_state(ctx)
        if state is not None:
            state.goal = None
        _VERIFY_CHECKS.pop(_key(ctx), None)
        return "Goal cleared."
    if args == "check":
        return _check(ctx)
    if args.lower().startswith(_VERIFY_PREFIX):
        command = args[len(_VERIFY_PREFIX) :].strip()
        if not command:
            return "Usage: /goal verify: <command>"
        _VERIFY_CHECKS.setdefault(_key(ctx), []).append(command)
        count = len(_VERIFY_CHECKS[_key(ctx)])
        return f"Attached verify command ({count} total): {command}"

    # Otherwise: set the objective.
    ctx.goal = args
    state = _engine_state(ctx)
    if state is not None:
        state.goal = args
    checks = _VERIFY_CHECKS.get(_key(ctx), [])
    suffix = f" ({len(checks)} verify check(s) attached)" if checks else ""
    return (
        f"Goal set: {args}{suffix}\n"
        "It is anchored into every turn. Run /goal check to test the stop-condition."
    )


def _show(ctx: CommandContext) -> str:
    goal = _current_goal(ctx)
    if not goal:
        return "No goal set. Usage: /goal <objective>"
    checks = _VERIFY_CHECKS.get(_key(ctx), [])
    lines = [f"Goal: {goal}"]
    if checks:
        lines.append("Verify checks:")
        lines += [f"  - {c}" for c in checks]
    else:
        lines.append("No verify checks attached (add with /goal verify: <cmd>).")
    return "\n".join(lines)


def _check(ctx: CommandContext) -> str:
    goal = _current_goal(ctx)
    if not goal:
        return "No goal set. Usage: /goal <objective>"
    checks = _VERIFY_CHECKS.get(_key(ctx), [])
    if not checks:
        return "No verify commands attached. Add one with /goal verify: <cmd>, then /goal check."
    workspace = resolve_workspace(ctx)
    schedule = ctx.extra.get("schedule")
    if workspace is None or schedule is None:
        return "Goal check needs a live session (workspace + scheduler)."
    settings = ctx.settings
    state = _engine_state(ctx)
    schedule(_run_check(goal, list(checks), workspace, settings, state))
    return f"Checking the goal against {len(checks)} verify command(s)…"


async def _run_check(goal: str, checks: list[str], workspace, settings, state) -> str:
    from ironcore.core.state import SessionState

    verifier = CommandVerifier(commands=checks)
    session = state if state is not None else SessionState()
    result = await verifier.verify(workspace, settings, session, touched_files=True)
    if result.ok:
        noun = "command" if len(checks) == 1 else "commands"
        return f"Goal stop-condition MET — all {len(checks)} verify {noun} passed.\nGoal: {goal}"
    return f"Goal stop-condition UNMET:\n{result.summary}"


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        "goal",
        "set a persistent objective + stop-condition for the session",
        "/goal <objective> | verify: <cmd> | check | show | clear",
        _cmd_goal,
    ),
)
