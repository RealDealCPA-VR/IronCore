"""Built-in slash commands and the default registry.

Live scaffold commands stay here: ``/help``, ``/version``, ``/mode``. The real
handlers live one-per-module and contribute a ``COMMANDS`` tuple this module
registers: ``/model``, ``/init``, ``/goal``, ``/loop``, ``/compact`` +
``/undo`` + ``/redo``, ``/review``, ``/memory``, ``/workflow``, and
``/envelope`` + ``/probe``. Every declared command is live — there are no
remaining stubs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ironcore import __version__
from ironcore.commands.base import CommandContext, CommandRegistry, SlashCommand
from ironcore.commands.envelopecmd import COMMANDS as _ENVELOPE_COMMANDS
from ironcore.commands.goalcmd import COMMANDS as _GOAL_COMMANDS
from ironcore.commands.initcmd import COMMANDS as _INIT_COMMANDS
from ironcore.commands.lifecyclecmd import COMMANDS as _LIFECYCLE_COMMANDS
from ironcore.commands.loopcmd import COMMANDS as _LOOP_COMMANDS
from ironcore.commands.memorycmd import COMMANDS as _MEMORY_COMMANDS
from ironcore.commands.modelcmd import COMMANDS as _MODEL_COMMANDS
from ironcore.commands.reviewcmd import COMMANDS as _REVIEW_COMMANDS
from ironcore.commands.workflowcmd import COMMANDS as _WORKFLOW_COMMANDS
from ironcore.safety.modes import CYCLE, DESCRIPTIONS, Mode, next_mode

if TYPE_CHECKING:
    from ironcore.plugins import LoadedPlugins

#: Every real command, in a stable display-friendly order.
_REAL_COMMANDS: tuple[SlashCommand, ...] = (
    *_MODEL_COMMANDS,
    *_INIT_COMMANDS,
    *_GOAL_COMMANDS,
    *_LOOP_COMMANDS,
    *_LIFECYCLE_COMMANDS,
    *_REVIEW_COMMANDS,
    *_MEMORY_COMMANDS,
    *_WORKFLOW_COMMANDS,
    *_ENVELOPE_COMMANDS,
)


def _cmd_help(ctx: CommandContext, args: str) -> str:
    # every ctx.extra key is optional (headless / alternate front ends may not
    # populate it); fall back to a freshly built registry rather than crash.
    registry: CommandRegistry = ctx.extra.get("registry") or build_default_registry()
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


def build_default_registry(plugins: LoadedPlugins | None = None) -> CommandRegistry:
    registry = CommandRegistry()
    mode_usage = "/mode [plan|manual|accept-edits|auto]"
    registry.register(SlashCommand("help", "list commands", "/help", _cmd_help))
    registry.register(SlashCommand("version", "show IronCore version", "/version", _cmd_version))
    registry.register(
        SlashCommand("mode", "cycle or set the operating mode", mode_usage, _cmd_mode)
    )
    for command in _REAL_COMMANDS:
        registry.register(command)
    if plugins is not None:
        # Plugin commands (MS-5) register after every builtin; builtins win a
        # name clash, and the skip is recorded for doctor/boot-note visibility.
        for command in plugins.commands:
            if registry.get(command.name) is not None:
                plugins.note_skip(
                    "ironcore.commands",
                    command.name,
                    f"duplicate of built-in command /{command.name}; built-ins win",
                )
                continue
            registry.register(command)
    return registry
