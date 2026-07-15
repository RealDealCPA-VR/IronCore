"""Slash commands: /goal, /loop, /workflow, /mode, and friends.

The registry and declared command set live in builtins.py; handlers land
across phase 8 (TODO.md IC-801..IC-807). /help works today and honestly
labels what is live vs. planned.
"""

from ironcore.commands.base import CommandContext, CommandRegistry, SlashCommand, UnknownCommand
from ironcore.commands.builtins import build_default_registry

__all__ = [
    "CommandContext",
    "CommandRegistry",
    "SlashCommand",
    "UnknownCommand",
    "build_default_registry",
]
