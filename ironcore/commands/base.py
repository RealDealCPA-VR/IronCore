"""Slash command contract and registry.

A command handler is synchronous, takes (CommandContext, args-string),
returns text for the transcript, and mutates only the context. Anything
long-running (loops, workflows) schedules work through the context and
returns immediately — commands never block the UI.

**Result type (CONTRACTS.md §6).** A handler returns ``str`` *or*
``rich.text.Text``. ``str`` is the default and stays the right answer for the
overwhelming majority of commands: a front end renders it however it renders
system output. ``Text`` exists for the handful of commands whose output
contains a *verdict* — ``/envelope``'s ladder (SELECTED / REJECTED / floor) and
``/goal check``'s met-or-not — where rendering every word at one weight loses
the single fact the reader came for. Such a handler builds the styling itself
and the front end renders it as-is.

SAFETY: a ``Text`` result must be composed programmatically —
``Text()`` plus ``.append(segment, style=...)`` — and **never** via
``Text.from_markup`` on anything derived from model, tool, file or config
content. The transcript's whole reason for wrapping dynamic output in ``Text``
is that such content can then never be reinterpreted as Rich console markup
(see ``tui/widgets/transcript.py``); a handler that parsed markup would punch a
hole straight through that guarantee. ``tests/test_report_card.py`` pins it.

Any consumer that needs a plain string (a log, a test, a non-TTY front end)
calls :func:`plain`, or ``.plain`` on the ``Text`` itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rich.text import Text

from ironcore.config.settings import Settings
from ironcore.safety.modes import Mode

#: What a slash command hands back: plain text, or pre-styled text.
CommandResult = str | Text

Handler = Callable[["CommandContext", str], CommandResult]


def plain(result: CommandResult) -> str:
    """The unstyled text of a command result, whichever form it took.

    The one function every ``str``-assuming consumer needs: styling is a
    rendering concern, and a log line, a session record or an assertion wants
    the characters, not the spans.
    """
    return result.plain if isinstance(result, Text) else result


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

    def dispatch(self, line: str, ctx: CommandContext) -> CommandResult:
        """Execute a raw '/name args...' line.

        Returns whatever the handler returned — ``str``, or a pre-styled
        ``Text``. Callers that need characters rather than spans wrap the
        result in :func:`plain`.
        """
        stripped = line.strip()
        if not stripped.startswith("/"):
            raise ValueError("not a slash command")
        name, _, args = stripped[1:].partition(" ")
        command = self.get(name)
        if command is None:
            raise UnknownCommand(name)
        return command.handler(ctx, args.strip())
