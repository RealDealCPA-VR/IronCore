"""Built-in slash commands.

Live today: /help, /mode, /version, /goal (set/show/clear state only —
the per-turn stop-condition check wires in with IC-803).
Everything else is declared with an honest "ships in IC-xxx" stub so
/help is complete from day one and the TUI can offer completion.
"""

from __future__ import annotations

from ironcore import __version__
from ironcore.commands.base import CommandContext, CommandRegistry, SlashCommand
from ironcore.safety.modes import CYCLE, DESCRIPTIONS, Mode, next_mode


def _cmd_help(ctx: CommandContext, args: str) -> str:
    registry: CommandRegistry = ctx.extra["registry"]
    lines = ["Commands:"]
    for cmd in registry.all():
        marker = "" if cmd.implemented else "  [planned]"
        lines.append(f"  /{cmd.name:<10} {cmd.summary}{marker}")
    return "\n".join(lines)


def _cmd_version(ctx: CommandContext, args: str) -> str:
    return f"IronCore v{__version__}"


def _cmd_mode(ctx: CommandContext, args: str) -> str:
    if args:
        try:
            ctx.mode = Mode(args.strip().lower())
        except ValueError:
            valid = ", ".join(m.value for m in CYCLE)
            return f"Unknown mode {args!r}. Valid: {valid}"
    else:
        ctx.mode = next_mode(ctx.mode)
    return f"Mode: {ctx.mode.value} — {DESCRIPTIONS[ctx.mode]}"


def _cmd_goal(ctx: CommandContext, args: str) -> str:
    if args == "clear":
        ctx.goal = None
        return "Goal cleared."
    if args:
        ctx.goal = args
        return (
            f"Goal set: {args}\n"
            "(Per-turn stop-condition enforcement ships in IC-803.)"
        )
    return f"Goal: {ctx.goal}" if ctx.goal else "No goal set. Usage: /goal <objective>"


def _stub(task_id: str) -> callable:
    def handler(ctx: CommandContext, args: str) -> str:
        return f"Not implemented yet — ships in {task_id} (see TODO.md)."

    return handler


#: (name, summary, usage, task-id that implements the real handler)
_PLANNED: tuple[tuple[str, str, str, str], ...] = (
    ("model", "switch model / list models at the endpoint", "/model [name]", "IC-801"),
    ("init", "scan the repo and generate IRONCORE.md", "/init", "IC-802"),
    ("loop", "run a prompt on an interval or self-paced", "/loop [interval] <prompt>", "IC-804"),
    ("compact", "compress history into a handoff-grade summary", "/compact", "IC-805"),
    ("undo", "revert the last change set (git snapshots)", "/undo", "IC-805"),
    ("review", "review the working diff for bugs", "/review", "IC-806"),
    ("memory", "view or edit project memory", "/memory", "IC-807"),
    ("workflow", "run a multi-agent workflow", "/workflow <name> [args]", "IC-904"),
    ("envelope", "show the current model's capability profile", "/envelope", "IC-608"),
    ("probe", "re-run capability probes for the current model", "/probe", "IC-608"),
)


def build_default_registry() -> CommandRegistry:
    registry = CommandRegistry()
    mode_usage = "/mode [plan|manual|accept-edits|auto]"
    registry.register(SlashCommand("help", "list commands", "/help", _cmd_help))
    registry.register(SlashCommand("version", "show IronCore version", "/version", _cmd_version))
    registry.register(
        SlashCommand("mode", "cycle or set the operating mode", mode_usage, _cmd_mode)
    )
    registry.register(
        SlashCommand(
            "goal",
            "set a persistent objective for the session",
            "/goal <objective> | /goal clear",
            _cmd_goal,
        )
    )
    for name, summary, usage, task_id in _PLANNED:
        registry.register(SlashCommand(name, summary, usage, _stub(task_id), implemented=False))
    return registry
