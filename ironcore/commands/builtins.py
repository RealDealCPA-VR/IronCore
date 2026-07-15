"""Built-in slash commands and the default registry.

Live scaffold commands stay here: ``/help``, ``/version``, ``/mode``. The real
phase-8 handlers (IC-801..807) live one-per-module and contribute a ``COMMANDS``
tuple that this module registers: ``/model``, ``/init``, ``/goal``, ``/loop``,
``/compact`` + ``/undo`` + ``/redo``, ``/review``, ``/memory``.

Only ``/workflow`` (IC-904), ``/envelope`` and ``/probe`` (IC-608) remain honest
"ships in IC-xxx" stubs, so ``/help`` is complete from day one and still labels
what is not yet live.
"""

from __future__ import annotations

from collections.abc import Callable

from ironcore import __version__
from ironcore.commands.base import CommandContext, CommandRegistry, SlashCommand
from ironcore.commands.goalcmd import COMMANDS as _GOAL_COMMANDS
from ironcore.commands.initcmd import COMMANDS as _INIT_COMMANDS
from ironcore.commands.lifecyclecmd import COMMANDS as _LIFECYCLE_COMMANDS
from ironcore.commands.loopcmd import COMMANDS as _LOOP_COMMANDS
from ironcore.commands.memorycmd import COMMANDS as _MEMORY_COMMANDS
from ironcore.commands.modelcmd import COMMANDS as _MODEL_COMMANDS
from ironcore.commands.reviewcmd import COMMANDS as _REVIEW_COMMANDS
from ironcore.safety.modes import CYCLE, DESCRIPTIONS, Mode, next_mode

#: Every real phase-8 command, in a stable display-friendly order.
_REAL_COMMANDS: tuple[SlashCommand, ...] = (
    *_MODEL_COMMANDS,
    *_INIT_COMMANDS,
    *_GOAL_COMMANDS,
    *_LOOP_COMMANDS,
    *_LIFECYCLE_COMMANDS,
    *_REVIEW_COMMANDS,
    *_MEMORY_COMMANDS,
)


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


def _stub(task_id: str) -> Callable[[CommandContext, str], str]:
    def handler(ctx: CommandContext, args: str) -> str:
        return f"Not implemented yet — ships in {task_id} (see TODO.md)."

    return handler


#: Commands still awaiting their owning task (name, summary, usage, task-id).
_PLANNED: tuple[tuple[str, str, str, str], ...] = (
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
    for command in _REAL_COMMANDS:
        registry.register(command)
    for name, summary, usage, task_id in _PLANNED:
        registry.register(SlashCommand(name, summary, usage, _stub(task_id), implemented=False))
    return registry
