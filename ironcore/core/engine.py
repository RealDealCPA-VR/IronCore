"""TurnEngine — TODO IC-501..IC-506. The heart of IronCore.

State machine for one turn (docs/ARCHITECTURE.md #3):

    COMPOSE   build the context window from harness-owned state:
              system prompt + anchors (goal, constraints, mode) +
              working-set files + compacted history + user input.
              The model NEVER has to remember; we re-present. (IC-501)
    CALL      stream from the provider using the envelope-selected
              tool protocol and sampling policy.
    PARSE     extract tool calls (native / strict JSON / IRONCALL text).
              Malformed output -> REPAIR: re-ask with the parse error
              framed as feedback, bounded retries. (IC-503)
    GATE      safety.policy.decide(mode, tool.risk); ask -> emit
              ApprovalRequired and await the front end; deny -> tell the
              model why and continue.
    EXECUTE   run the tool; truncate + redact output. (IC-502)
    OBSERVE   append result; loop to CALL until the model stops
              requesting tools or budgets trip. (IC-506)
    VERIFY    after WRITE/EXEC activity: run the project's verify
              command(s), feed failures back once, then surface. (IC-504)
    DONE      emit TurnCompleted with usage + stop_reason.

Invariants (frozen — docs/CONTRACTS.md #Engine):
* No tool executes without a GATE decision.
* Every provider call goes through the context composer — no ad-hoc
  message lists.
* The engine is UI-agnostic: it emits core.events and awaits approval
  futures; it never prints or prompts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from ironcore.config.settings import Settings
from ironcore.core.events import Event
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import Provider
from ironcore.safety.modes import Mode
from ironcore.tools.base import ToolRegistry


class TurnEngine:
    """See module docstring. Ships across IC-501..IC-506."""

    def __init__(
        self,
        provider: Provider,
        tools: ToolRegistry,
        settings: Settings,
        profile: CapabilityProfile,
        mode: Mode = Mode.MANUAL,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.settings = settings
        self.profile = profile
        self.mode = mode

    async def run_turn(self, user_input: str) -> AsyncIterator[Event]:
        """Drive one user turn to completion, yielding events as they occur."""
        raise NotImplementedError("IC-502: turn state machine (see TODO.md)")
        yield  # pragma: no cover — marks this as an async generator
