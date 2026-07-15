"""PlanStepPlanner (IC-505): evidence-gated advance, completion transitions,
plan loading, and the composer anchor surfacing the current micro-step (SPEC §5.3)."""

from ironcore.config.settings import Settings
from ironcore.core.composer import compose
from ironcore.core.state import SessionState
from ironcore.core.steps import PlanStepPlanner, set_plan
from ironcore.envelope.profile import CapabilityProfile

SYS = "You are IronCore, a terminal coding agent."


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="test-model", honest_context=4096, coherence_horizon=6)


def _anchor_blob(state: SessionState) -> str:
    """Full composed message text, for asserting the anchor surfaces a step."""
    messages = compose(
        state,
        profile=_profile(),
        settings=Settings(),
        system_prompt=SYS,
        working_set={},
        history=[],
        user_input="continue",
    )
    return "\n".join(m.content for m in messages)


# -- set_plan ------------------------------------------------------------------


def test_set_plan_loads_steps_and_resets_progress():
    state = SessionState(plan_cursor=5, plan_evidence={0: "stale"})
    set_plan(state, ["read file", "edit file", "run tests"])
    assert state.plan_steps == ["read file", "edit file", "run tests"]
    assert state.plan_cursor == 0
    assert state.plan_evidence == {}


def test_set_plan_copies_the_iterable():
    state = SessionState()
    steps = ["one", "two"]
    set_plan(state, steps)
    steps.append("three")  # mutating the caller's list must not touch the plan
    assert state.plan_steps == ["one", "two"]


# -- advance: evidence gating --------------------------------------------------


def test_advance_records_evidence_at_cursor_and_increments():
    state = SessionState()
    set_plan(state, ["a", "b", "c"])
    PlanStepPlanner().advance(state, "pytest: 3 passed")
    assert state.plan_cursor == 1
    assert state.plan_evidence == {0: "pytest: 3 passed"}


def test_empty_evidence_does_not_advance():
    state = SessionState()
    set_plan(state, ["a", "b"])
    planner = PlanStepPlanner()
    planner.advance(state, "")  # empty -> no-op
    planner.advance(state, "   \n\t ")  # whitespace-only -> no-op
    assert state.plan_cursor == 0
    assert state.plan_evidence == {}


def test_advance_no_plan_is_a_noop():
    state = SessionState()  # no plan loaded
    PlanStepPlanner().advance(state, "some evidence")
    assert state.plan_cursor == 0
    assert state.plan_evidence == {}


def test_advance_clamps_at_end_and_never_overruns():
    state = SessionState()
    set_plan(state, ["only"])
    planner = PlanStepPlanner()
    planner.advance(state, "done it")
    assert state.plan_cursor == 1
    planner.advance(state, "more evidence past the end")
    assert state.plan_cursor == 1  # clamped, evidence not recorded past the end
    assert set(state.plan_evidence) == {0}


def test_advance_normalizes_a_stray_negative_cursor():
    state = SessionState(plan_steps=["a", "b"], plan_cursor=-3)
    PlanStepPlanner().advance(state, "recover")
    assert state.plan_cursor == 1
    assert state.plan_evidence == {0: "recover"}


# -- is_complete transitions ---------------------------------------------------


def test_is_complete_true_for_empty_plan():
    assert PlanStepPlanner().is_complete(SessionState()) is True


def test_is_complete_transitions_through_the_plan():
    state = SessionState()
    set_plan(state, ["a", "b"])
    planner = PlanStepPlanner()
    assert planner.is_complete(state) is False
    planner.advance(state, "did a")
    assert planner.is_complete(state) is False
    planner.advance(state, "did b")
    assert planner.is_complete(state) is True


# -- composer anchor surfaces the current step (accept criterion) --------------


def test_composer_anchor_surfaces_current_step_and_advances():
    state = SessionState()
    set_plan(state, ["read config", "patch handler", "run tests"])

    blob = _anchor_blob(state)
    assert "step 1 of 3 — read config" in blob

    PlanStepPlanner().advance(state, "opened config; 42 lines")
    blob = _anchor_blob(state)
    assert "step 2 of 3 — patch handler" in blob
    assert "Completed: step 1" in blob  # evidence-derived completed note


def test_composer_anchor_reports_all_steps_complete():
    state = SessionState()
    set_plan(state, ["only step"])
    PlanStepPlanner().advance(state, "finished")
    blob = _anchor_blob(state)
    assert "all 1 steps complete" in blob


# -- a plan carried through save/load stays consistent -------------------------


def test_plan_round_trips_through_state_serialization():
    state = SessionState()
    set_plan(state, ["a", "b"])
    PlanStepPlanner().advance(state, "did a")
    restored = SessionState.from_dict(state.to_dict())
    assert restored.plan_steps == ["a", "b"]
    assert restored.plan_cursor == 1
    assert restored.plan_evidence == {0: "did a"}
    assert isinstance(next(iter(restored.plan_evidence)), int)  # keys came back as ints


def test_planner_advances_a_restored_plan():
    """A plan loaded via set_plan then persisted still advances after reload."""
    state = SessionState()
    set_plan(state, ["a", "b"])
    restored = SessionState.from_dict(state.to_dict())
    PlanStepPlanner().advance(restored, "evidence")
    assert restored.plan_cursor == 1
