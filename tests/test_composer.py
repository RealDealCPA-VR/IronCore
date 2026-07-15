"""Context composer (IC-501): determinism, budget invariant, anchor cadence,
micro-step surfacing, working-set truncation/MRU-drop, and redaction."""

from ironcore.config.settings import Settings
from ironcore.core.composer import (
    ANCHOR_SHARE,
    FILE_MARKER,
    HISTORY_SHARE,
    RESPONSE_HEADROOM_SHARE,
    SYSTEM_SHARE,
    WORKING_SET_SHARE,
    compose,
    estimate_tokens,
    should_anchor,
)
from ironcore.core.state import SessionState
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import Message
from ironcore.safety.modes import Mode

SYS = "You are IronCore, a terminal coding agent."


def _profile(honest_context: int = 4096, coherence_horizon: int = 6) -> CapabilityProfile:
    return CapabilityProfile(
        model_id="test-model",
        honest_context=honest_context,
        coherence_horizon=coherence_horizon,
    )


def _content_tokens(messages: list[Message]) -> int:
    return sum(estimate_tokens(m.content) for m in messages)


def _budget_ceiling(profile: CapabilityProfile) -> int:
    return profile.honest_context - int(profile.honest_context * RESPONSE_HEADROOM_SHARE)


def _compose(state: SessionState, profile: CapabilityProfile, **kw) -> list[Message]:
    defaults = dict(
        profile=profile,
        settings=Settings(),
        system_prompt=SYS,
        working_set={},
        history=[],
        user_input="do the thing",
        memory="",
    )
    defaults.update(kw)
    return compose(state, **defaults)


# -- shares & estimator --------------------------------------------------------


def test_budget_shares_sum_to_one():
    total = (
        SYSTEM_SHARE
        + ANCHOR_SHARE
        + WORKING_SET_SHARE
        + HISTORY_SHARE
        + RESPONSE_HEADROOM_SHARE
    )
    assert abs(total - 1.0) < 1e-9


def test_estimate_tokens_is_ceil_chars_over_four():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


# -- structure & determinism ---------------------------------------------------


def test_message_order_and_roles():
    state = SessionState(turn_count=0)
    msgs = _compose(
        state,
        _profile(),
        working_set={"a.py": "print('a')"},
        history=[Message(role="assistant", content="earlier reply")],
    )
    # system, anchor(system, turn 0), working-set(user), history(assistant), input(user)
    assert msgs[0].role == "system" and SYS in msgs[0].content
    assert msgs[1].role == "system" and "Goal" in msgs[1].content  # the anchor
    assert msgs[2].role == "user" and "a.py" in msgs[2].content
    assert msgs[3].role == "assistant" and "earlier reply" in msgs[3].content
    assert msgs[-1].role == "user" and "do the thing" in msgs[-1].content


def test_deterministic_same_inputs_same_output():
    state = SessionState(
        mode=Mode.ACCEPT_EDITS,
        goal="ship IC-501",
        plan_steps=["compose", "test", "verify"],
        plan_cursor=1,
        plan_evidence={0: "compose written"},
        turn_count=3,
    )
    ws = {"b.py": "x = 1\n" * 40, "a.py": "y = 2\n" * 40}
    hist = [Message(role="user", content="hi"), Message(role="assistant", content="ok")]
    first = _compose(state, _profile(), working_set=dict(ws), history=list(hist))
    second = _compose(state, _profile(), working_set=dict(ws), history=list(hist))
    assert first == second


def test_compose_does_not_mutate_inputs():
    ws = {"a.py": "content"}
    hist = [Message(role="assistant", content="reply")]
    state = SessionState(turn_count=0)
    _compose(state, _profile(), working_set=ws, history=hist)
    assert ws == {"a.py": "content"}
    assert hist == [Message(role="assistant", content="reply")]


def test_empty_user_input_yields_no_trailing_user_message():
    # tool-observe re-calls (IC-502) pass "" — the pending work lives in history.
    msgs = _compose(SessionState(turn_count=0), _profile(), user_input="")
    assert msgs[-1].content != ""
    assert all("do the thing" not in m.content for m in msgs)


# -- budget invariant (property over hand-built varied states) -----------------


def test_total_estimate_never_exceeds_budget():
    profiles = [_profile(256), _profile(1024), _profile(4096, 12), _profile(8192, 2)]
    for hc_i, profile in enumerate(profiles):
        for seed in range(6):
            n = (seed * 7 + hc_i * 3) % 11
            state = SessionState(
                mode=list(Mode)[seed % len(Mode)],
                goal=("g" * (seed * 50)) or None,
                plan_steps=[f"step {j}: {'s' * (j * 30)}" for j in range(n)],
                plan_cursor=seed % (n + 1),
                plan_evidence={0: "e" * (seed * 40)} if n else {},
                turn_count=seed,
            )
            working_set = {
                f"file{k}.py": f"line {k}\n" * (seed * 20 + k * 10) for k in range(n % 5 + 1)
            }
            history = [
                Message(role="user" if j % 2 == 0 else "assistant", content="h" * (j * 25 + seed))
                for j in range(n)
            ]
            msgs = _compose(
                state,
                profile,
                working_set=working_set,
                history=history,
                user_input="u" * (seed * 33),
                memory="m" * (seed * 45),
            )
            assert _content_tokens(msgs) <= _budget_ceiling(profile), (hc_i, seed)


# -- anchor cadence rule -------------------------------------------------------


def _has_anchor(msgs: list[Message]) -> bool:
    # the anchor is the system message carrying the standing-context header
    return any(m.role == "system" and "Standing context" in m.content for m in msgs)


def test_should_anchor_rule():
    assert should_anchor(0, 6) is True  # first turn always
    assert should_anchor(6, 6) is True  # multiple of cadence
    assert should_anchor(12, 6) is True
    assert should_anchor(3, 6) is False  # off-cadence
    assert should_anchor(5, 6) is False
    assert should_anchor(7, 1) is True  # degenerate cadence guarded


def test_anchor_present_on_turn_zero_and_cadence():
    profile = _profile(coherence_horizon=6)  # cadence == 6
    assert _has_anchor(_compose(SessionState(turn_count=0), profile))
    assert _has_anchor(_compose(SessionState(turn_count=6), profile))
    assert _has_anchor(_compose(SessionState(turn_count=12), profile))


def test_anchor_absent_off_cadence_without_plan():
    profile = _profile(coherence_horizon=6)
    assert not _has_anchor(_compose(SessionState(turn_count=3), profile))
    assert not _has_anchor(_compose(SessionState(turn_count=5), profile))


def test_plan_active_forces_anchor_off_cadence():
    profile = _profile(coherence_horizon=6)
    state = SessionState(turn_count=5, plan_steps=["a", "b"], plan_cursor=0)
    assert _has_anchor(_compose(state, profile))


# -- micro-step surfacing (IC-505) ---------------------------------------------


def _anchor(msgs: list[Message]) -> str:
    return next(m.content for m in msgs if m.role == "system" and "Standing context" in m.content)


def test_anchor_surfaces_current_plan_step():
    state = SessionState(
        goal="ship it",
        plan_steps=["read spec", "write code", "run tests"],
        plan_cursor=1,
        plan_evidence={0: "spec read; notes captured\nsecond line ignored"},
        turn_count=0,
    )
    anchor = _anchor(_compose(state, _profile()))
    assert "step 2 of 3 — write code" in anchor
    assert "ship it" in anchor
    assert "Completed: step 1 (spec read; notes captured)" in anchor  # one-line note
    assert "second line ignored" not in anchor


def test_anchor_reports_all_steps_complete():
    state = SessionState(plan_steps=["a", "b"], plan_cursor=2, turn_count=0)
    assert "all 2 steps complete" in _anchor(_compose(state, _profile()))


def test_anchor_states_goal_mode_and_constraints():
    state = SessionState(mode=Mode.PLAN, goal="explore repo", turn_count=0)
    anchor = _anchor(_compose(state, _profile()))
    assert "Goal: explore repo" in anchor
    assert "Mode: plan" in anchor
    assert "PLAN mode" in anchor  # mode-derived constraint surfaced


# -- working set: MRU order, truncation marker, drop least-recent --------------


def test_working_set_mru_order_when_budget_is_ample():
    ws = {"recent.py": "AAA", "older.py": "BBB"}  # dict order == MRU
    msgs = _compose(SessionState(turn_count=0), _profile(), working_set=ws)
    block = next(m.content for m in msgs if m.role == "user" and "recent.py" in m.content)
    assert block.index("recent.py") < block.index("older.py")


def test_oversize_file_truncated_and_least_recent_dropped():
    # honest_context 400 -> working-set budget = 160 tokens (~640 chars).
    ws = {
        "recent.py": "R" * 4000,  # far bigger than the whole working-set budget
        "OLDER_UNIQUE.py": "keep-me-please",  # least-recent -> must be dropped
    }
    msgs = _compose(SessionState(turn_count=0), _profile(honest_context=400), working_set=ws)
    block = next(m.content for m in msgs if m.role == "user" and "recent.py" in m.content)
    assert FILE_MARKER in block  # recent file truncated with an honest marker
    assert "OLDER_UNIQUE" not in block  # least-recent dropped entirely
    ws_budget = int(400 * WORKING_SET_SHARE)
    assert estimate_tokens(block) <= ws_budget


def test_history_keeps_most_recent_tail_within_budget():
    profile = _profile(honest_context=400)  # history region = 100 tokens
    history = [Message(role="user", content=f"MSG{i}-" + "z" * 200) for i in range(10)]
    msgs = _compose(SessionState(turn_count=0), profile, history=history, user_input="")
    hist_msgs = [m for m in msgs if m.content.startswith("MSG")]
    assert hist_msgs, "expected at least one history message to fit"
    # the most-recent message survives; the oldest is dropped
    assert any("MSG9-" in m.content for m in hist_msgs)
    assert all("MSG0-" not in m.content for m in hist_msgs)


# -- redaction (choke point 1) -------------------------------------------------

SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUVWX0123456789"


def test_working_set_content_is_redacted():
    ws = {"config.py": f'API_KEY = "{SECRET}"'}
    msgs = _compose(SessionState(turn_count=0), _profile(), working_set=ws)
    joined = "".join(m.content for m in msgs)
    assert SECRET not in joined
    assert "[redacted:openai-key]" in joined


def test_history_content_is_redacted():
    history = [Message(role="assistant", content=f"here is the key {SECRET} enjoy")]
    msgs = _compose(SessionState(turn_count=0), _profile(), history=history)
    joined = "".join(m.content for m in msgs)
    assert SECRET not in joined
    assert "[redacted:openai-key]" in joined


def test_system_prompt_and_user_input_are_not_redacted():
    # trusted surfaces stay verbatim (the redactor would otherwise mangle them).
    msgs = _compose(
        SessionState(turn_count=0),
        _profile(),
        system_prompt=f"{SYS} token={SECRET}",
        user_input=f"use {SECRET} now",
        memory=f"remember {SECRET}",
    )
    system = msgs[0].content
    user_in = msgs[-1].content
    assert SECRET in system  # system_prompt + memory are trusted
    assert SECRET in user_in  # live user input is trusted
