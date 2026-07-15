"""PlanStepPlanner (IC-505): evidence-gated micro-stepping (SPEC §5.3).

For multi-step work the harness holds the plan and feeds the model **one step at
a time**, keeping the current step visible in the composer anchor (SPEC §5.3).
The MODEL executes; the HARNESS advances the cursor — and it advances on
*evidence*, never on a bare model claim. A model that says "done" without a tool
result, test tail, or command output behind it does not move the plan forward.

This module provides:

* :class:`PlanStepPlanner` — the :class:`~ironcore.core.protocols.StepPlanner`
  implementation the engine swaps in for the placeholder ``LinearStepPlanner``.
* :func:`set_plan` — the mechanism to *load* a plan into ``SessionState`` (steps,
  cursor reset to 0, evidence cleared). Plan CREATION from a goal (decomposing a
  free-form goal into steps) is IC-803's job via ``/goal``; here we only provide
  the load-and-advance machinery a created plan runs on.

Stdlib only, no clocks, no randomness — advancing a cursor is pure state
mutation, so this module is fully deterministic and offline-testable. State is
mutated IN PLACE on the passed :class:`~ironcore.core.state.SessionState`
(the same contract the ``StepPlanner`` Protocol documents).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ironcore.core.protocols import StepPlanner

if TYPE_CHECKING:  # annotations only — no runtime coupling to state.py
    from collections.abc import Iterable

    from ironcore.core.state import SessionState

__all__ = ["PlanStepPlanner", "set_plan"]


def set_plan(state: SessionState, steps: Iterable[str]) -> None:
    """Load ``steps`` into ``state`` as a fresh plan and reset progress.

    Sets ``plan_steps`` to a new list (each coerced to ``str``), rewinds
    ``plan_cursor`` to 0, and clears ``plan_evidence``. This is the mechanism the
    orchestrator (IC-803 ``/goal``) uses to install a decomposed plan; the
    composer's anchor then surfaces the current step every turn while the plan is
    active. Callers should pass meaningful, non-empty step strings — the anchor
    renders each verbatim.
    """
    state.plan_steps = [str(step) for step in steps]
    state.plan_cursor = 0
    state.plan_evidence = {}


class PlanStepPlanner(StepPlanner):
    """Advance one micro-step per piece of real evidence, in order (SPEC §5.3).

    The engine calls :meth:`advance` after tool activity with the evidence for the
    current step (a command tail, test result, or tool output). Advancing is
    **evidence-gated**: only a non-empty, non-whitespace ``evidence`` records the
    step complete and moves the cursor forward. Empty evidence — a bare model
    claim with nothing behind it — is a no-op, so the harness never lets the model
    talk its way past a step it has not actually finished.

    The cursor is clamped to ``len(plan_steps)`` and never advances past the end;
    :meth:`is_complete` reports when the whole plan is done (or when there is no
    plan at all). Replaces the placeholder ``LinearStepPlanner`` verbatim.
    """

    def advance(self, state: SessionState, evidence: str) -> None:
        """Record ``evidence`` for the current step and move to the next one.

        Evidence-gated: if ``evidence`` is empty or whitespace-only, do nothing —
        neither record nor advance (a step advances on evidence, not on a claim).
        With real evidence, store it at the current cursor and increment, clamped
        so the cursor never runs past ``len(plan_steps)``. A no-op when there is no
        plan or the plan is already complete.
        """
        steps = state.plan_steps
        if not steps:
            return
        cursor = state.plan_cursor
        if cursor < 0:
            cursor = 0
        if cursor >= len(steps):
            state.plan_cursor = len(steps)  # clamp a stray cursor; already complete
            return
        if not evidence or not evidence.strip():
            return  # evidence-gated: no real evidence -> no record, no advance
        state.plan_evidence[cursor] = evidence
        state.plan_cursor = min(cursor + 1, len(steps))

    def is_complete(self, state: SessionState) -> bool:
        """``True`` when the plan is finished or absent.

        A non-empty plan is complete once the cursor has passed its last step;
        an empty plan is trivially complete (there is nothing to do).
        """
        steps = state.plan_steps
        return not steps or state.plan_cursor >= len(steps)
