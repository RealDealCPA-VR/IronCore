"""Event vocabulary between the turn engine and any front end.

CONTRACT (docs/CONTRACTS.md #Events): the TUI, headless mode, and tests
all consume this stream; nothing else crosses the core/tui boundary.
Additive changes only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ironcore.providers.base import ToolCall
from ironcore.tools.base import ToolResult


@dataclass
class TurnStarted:
    turn_id: str
    mode: str


@dataclass
class TextDelta:
    turn_id: str
    text: str


@dataclass
class ToolCallRequested:
    turn_id: str
    call: ToolCall
    risk: str
    decision: str  # allow | ask | deny (from safety.policy.decide)


@dataclass
class ApprovalRequired:
    """Emitted when decision == ask. The front end must answer via the
    engine's approval future; the engine blocks this call until then."""

    turn_id: str
    call: ToolCall
    risk: str
    preview: str  # human-readable: the diff, the command line, the URL


@dataclass
class ToolCallFinished:
    turn_id: str
    call: ToolCall
    result: ToolResult


@dataclass
class TurnCompleted:
    turn_id: str
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "done"  # done | budget | denied | error | goal-unmet


@dataclass
class TurnError:
    turn_id: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


Event = (
    TurnStarted
    | TextDelta
    | ToolCallRequested
    | ApprovalRequired
    | ToolCallFinished
    | TurnCompleted
    | TurnError
)
