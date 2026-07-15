"""Slash command contract and registry.

A command handler is synchronous, takes (CommandContext, args-string),
returns text for the transcript, and mutates only the context. Anything
long-running (loops, workflows) schedules work through the context and
returns immediately — commands never block the UI.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ironcore.config.settings import Settings
from ironcore.safety.modes import Mode

Handler = Callable[["CommandContext", str], str]


class UnknownCommand(KeyError):
    pass


@dataclass
class CommandContext:
    """Mutable session state a command may read or change."""

    settings: Settings
    mode: Mode = Mode.MANUAL
    goal: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SlashCommand:
    name: str  # without the leading slash
    summary: str
    usage: str
    handler: Handler
    implemented: bool = True


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        if command.name in self._commands:
            raise ValueError(f"duplicate command: /{command.name}")
        self._commands[command.name] = command

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name.lstrip("/"))

    def all(self) -> list[SlashCommand]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    def dispatch(self, line: str, ctx: CommandContext) -> str:
        """Execute a raw '/name args...' line."""
        stripped = line.strip()
        if not stripped.startswith("/"):
            raise ValueError("not a slash command")
        name, _, args = stripped[1:].partition(" ")
        command = self.get(name)
        if command is None:
            raise UnknownCommand(name)
        return command.handler(ctx, args.strip())
