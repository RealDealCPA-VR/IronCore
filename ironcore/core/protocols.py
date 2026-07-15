"""Collaborator seams for the turn engine (SPEC §5.4/5.5/5.6/5.3).

The :class:`~ironcore.core.engine.TurnEngine` orchestrates the turn loop but
delegates four judgement calls to pluggable collaborators, each defined here as
a small ``Protocol`` plus a minimal default implementation. The engine is fully
runnable and testable TODAY on the defaults; the phase-5 refinement tasks
replace them verbatim:

* :class:`RepairPolicy`  → IC-503 (repair loops / ladder-down)
* :class:`Verifier`      → IC-504 (verification loop)
* :class:`BudgetTracker` → IC-506 (budgets + runaway protection)
* :class:`StepPlanner`   → IC-505 (micro-stepping)

Each Protocol's docstring is the contract IC-503..506 program against. Keep the
defaults SMALL — they exist to make the engine honest and self-testing, not to
pre-empt the real implementations. Stdlib only; no engine import (the engine
imports THIS module, never the reverse).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # annotations only — avoids any runtime coupling / import cycle
    from pathlib import Path

    from ironcore.config.settings import Settings
    from ironcore.core.state import SessionState

__all__ = [
    "RepairAction",
    "RepairPolicy",
    "DefaultRepairPolicy",
    "VerifyResult",
    "Verifier",
    "NoopVerifier",
    "BudgetTracker",
    "DefaultBudget",
    "StepPlanner",
    "LinearStepPlanner",
]


# --------------------------------------------------------------------------- #
# Repair policy (SPEC §5.4) — IC-503
# --------------------------------------------------------------------------- #


class RepairAction(StrEnum):
    """What the engine should do about a malformed tool call / unappliable edit.

    * ``RETRY`` — re-ask on the SAME ladder rung with the mechanical error framed
      as feedback (bump sampling temperature via ``attempt``).
    * ``LADDER_DOWN`` — drop to the tool-call floor (``text_protocol``) for the
      rest of the turn, then re-ask.
    * ``GIVE_UP`` — stop repairing; the turn ends with ``stop_reason="error"``.
    """

    RETRY = "retry"
    LADDER_DOWN = "ladder_down"
    GIVE_UP = "give_up"


class RepairPolicy(Protocol):
    """Decides how to recover from a malformed model output (SPEC §5.4).

    ``decide`` is called once per malformed CALL with:

    * ``attempt`` — repair attempts already made THIS turn (0 on the first).
    * ``error``   — the mechanical, model-facing error string (parser message /
      patcher reason). This is what gets re-presented on a RETRY.
    * ``raw``     — the raw offending text (accumulated completion / fragment).
    * ``rung``    — the active tool-call ladder rung (e.g. ``"native"``).

    It returns a :class:`RepairAction`. The engine owns the budget: it increments
    ``attempt`` after a RETRY/LADDER_DOWN and never calls ``decide`` past the
    per-turn repair cap.
    """

    def decide(self, *, attempt: int, error: str, raw: str, rung: str) -> RepairAction: ...


class DefaultRepairPolicy:
    """Retry once, then give up (SPEC §5.4's minimum). IC-503 adds LADDER_DOWN.

    ``attempt < max_retries`` → RETRY, else GIVE_UP. Never emits LADDER_DOWN;
    the engine still HANDLES it so a smarter policy can drop it in unchanged.
    """

    def __init__(self, *, max_retries: int = 1) -> None:
        self.max_retries = max_retries

    def decide(self, *, attempt: int, error: str, raw: str, rung: str) -> RepairAction:
        return RepairAction.RETRY if attempt < self.max_retries else RepairAction.GIVE_UP


# --------------------------------------------------------------------------- #
# Verifier (SPEC §5.5) — IC-504
# --------------------------------------------------------------------------- #


@dataclass
class VerifyResult:
    """Outcome of the post-mutation verification pass.

    * ``ok`` — did every verify command pass? ``False`` folds ``summary`` into
      the turn result honestly (the engine can never report unverified work as
      done — SPEC §5.5).
    * ``summary`` — human/model-facing one-liner (e.g. ``"2 tests failing"``).
    * ``ran`` — the commands actually executed (``[]`` = nothing configured).
    """

    ok: bool
    summary: str = ""
    ran: list[str] = field(default_factory=list)


class Verifier(Protocol):
    """Runs the project's verify commands after a WRITE/EXEC turn (SPEC §5.5).

    ``verify`` is awaited only when the turn produced mutations; ``touched_files``
    is ``True`` when a WRITE tool ran (files changed on disk) versus EXEC-only.
    Returns a :class:`VerifyResult`. Implementations must be side-effect-free
    beyond running the declared verify commands.
    """

    async def verify(
        self,
        workspace: Path,
        settings: Settings,
        state: SessionState,
        touched_files: bool,
    ) -> VerifyResult: ...


class NoopVerifier:
    """No verify commands configured: nothing to run, nothing to report."""

    async def verify(
        self,
        workspace: Path,
        settings: Settings,
        state: SessionState,
        touched_files: bool,
    ) -> VerifyResult:
        return VerifyResult(ok=True, summary="", ran=[])


# --------------------------------------------------------------------------- #
# Budget tracker (SPEC §5.6) — IC-506
# --------------------------------------------------------------------------- #


class BudgetTracker(Protocol):
    """Per-turn budgets + runaway protection (SPEC §5.6).

    Lifecycle the engine drives:

    * ``start_turn()``            — reset per-turn counters.
    * ``record_call(tokens)``     — after each provider call, with its token cost.
    * ``check() -> str | None``   — a ``stop_reason`` if a cap tripped, checked
      before every provider call; ``None`` means keep going.
    * ``note_tool(name, args)``   — before each tool call; returns a ``stop_reason``
      when the loop detector fires (same tool+args too many times), else ``None``.
    * ``should_continue() -> bool`` — cheap "are we still under budget?" for
      ``sampling.best_of`` and other inner loops.

    A returned ``stop_reason`` is what lands on ``TurnCompleted.stop_reason``
    verbatim; both caps here report ``"budget"`` (runaway protection is a budget
    concern and the event vocabulary has no ``"loop"``).
    """

    def start_turn(self) -> None: ...
    def record_call(self, tokens: int) -> None: ...
    def check(self) -> str | None: ...
    def note_tool(self, name: str, args: dict[str, Any]) -> str | None: ...
    def should_continue(self) -> bool: ...


def _canonical_args(args: Any) -> str:
    """Stable string form of tool args for loop-detection equality."""
    try:
        return json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(args)


class DefaultBudget:
    """A max-provider-calls cap plus an identical-tool-call loop detector.

    * ``max_provider_calls`` provider calls per turn, then ``"budget"``.
    * the same ``(tool, args)`` ``loop_limit`` times in a row → ``"budget"``
      (SPEC §5.6: twice = intervention, three times = stop). Token spend is
      accumulated for reporting but not capped here — IC-506 adds wall-clock and
      token caps.
    """

    def __init__(self, *, max_provider_calls: int = 20, loop_limit: int = 3) -> None:
        self.max_provider_calls = max_provider_calls
        self.loop_limit = loop_limit
        self._calls = 0
        self._tokens = 0
        self._last_key: tuple[str, str] | None = None
        self._repeat = 0

    def start_turn(self) -> None:
        self._calls = 0
        self._tokens = 0
        self._last_key = None
        self._repeat = 0

    def record_call(self, tokens: int) -> None:
        self._calls += 1
        self._tokens += max(0, int(tokens))

    def check(self) -> str | None:
        return "budget" if self._calls >= self.max_provider_calls else None

    def note_tool(self, name: str, args: dict[str, Any]) -> str | None:
        key = (name, _canonical_args(args))
        if key == self._last_key:
            self._repeat += 1
        else:
            self._last_key = key
            self._repeat = 1
        return "budget" if self._repeat >= self.loop_limit else None

    def should_continue(self) -> bool:
        return self.check() is None


# --------------------------------------------------------------------------- #
# Step planner (SPEC §5.3) — IC-505
# --------------------------------------------------------------------------- #


class StepPlanner(Protocol):
    """Holds a plan and advances one micro-step at a time (SPEC §5.3).

    The engine feeds the model one step (visible in the composer anchor); the
    HARNESS advances the cursor on evidence, never the model. ``advance`` records
    ``evidence`` for the current step and moves to the next; ``is_complete`` is
    ``True`` once the cursor passes the last step. State mutation is in-place on
    the passed :class:`~ironcore.core.state.SessionState`.
    """

    def advance(self, state: SessionState, evidence: str) -> None: ...
    def is_complete(self, state: SessionState) -> bool: ...


class LinearStepPlanner:
    """Advance the plan cursor by one on each piece of evidence, in order.

    Records ``evidence`` against the current step index, then increments the
    cursor. Complete when the cursor reaches ``len(plan_steps)`` — which is
    trivially true when no plan is active (the engine only calls ``advance``
    when a plan exists). IC-505 replaces this with real step-completion logic.
    """

    def advance(self, state: SessionState, evidence: str) -> None:
        steps = state.plan_steps
        cursor = state.plan_cursor
        if steps and 0 <= cursor < len(steps):
            state.plan_evidence[cursor] = evidence
            state.plan_cursor = cursor + 1

    def is_complete(self, state: SessionState) -> bool:
        return state.plan_cursor >= len(state.plan_steps)
