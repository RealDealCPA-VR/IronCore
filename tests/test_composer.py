"""Context composer (IC-501): determinism, budget invariant, anchor cadence,
micro-step surfacing, working-set truncation/MRU-drop, and redaction."""

from pathlib import Path

from ironcore.config.settings import Settings
from ironcore.core.composer import (
    ANCHOR_SHARE,
    FILE_MARKER,
    HISTORY_SHARE,
    MEMORY_MARKER,
    RESPONSE_HEADROOM_SHARE,
    SYSTEM_SHARE,
    WORKING_SET_SHARE,
    compose,
    estimate_tokens,
    load_project_memory,
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


# -- off-cadence goal re-presentation (engine M1) ------------------------------


def _goal_line(msgs: list[Message]) -> str | None:
    return next(
        (
            m.content
            for m in msgs
            if m.role == "system" and m.content.startswith("Goal (re-presented")
        ),
        None,
    )


def test_off_cadence_goal_is_re_presented_without_the_full_anchor():
    profile = _profile(coherence_horizon=6)  # cadence 6
    state = SessionState(turn_count=3, goal="make fib() correct")  # off-cadence, no plan
    msgs = _compose(state, profile)
    assert not _has_anchor(msgs)  # the full standing-context block is NOT injected
    line = _goal_line(msgs)
    assert line is not None and "make fib() correct" in line  # but the goal still is


def test_off_cadence_no_goal_line_when_no_goal():
    profile = _profile(coherence_horizon=6)
    msgs = _compose(SessionState(turn_count=3), profile)  # no goal set
    assert _goal_line(msgs) is None
    assert not _has_anchor(msgs)


def test_goal_line_and_full_anchor_are_mutually_exclusive():
    profile = _profile(coherence_horizon=6)
    # turn 0 → full anchor present, so the compact goal line must NOT also appear
    msgs = _compose(SessionState(turn_count=0, goal="ship it"), profile)
    assert _has_anchor(msgs)
    assert _goal_line(msgs) is None
    # exactly one system message carries the goal (the anchor), not two
    goal_bearing = [m for m in msgs if m.role == "system" and "ship it" in m.content]
    assert len(goal_bearing) == 1


def test_budget_invariant_holds_with_off_cadence_goal_line():
    for profile in (_profile(256), _profile(1024), _profile(4096, 12)):
        msgs = _compose(
            SessionState(turn_count=3, goal="g" * 4000),  # oversize goal, off-cadence
            profile,
            working_set={"f.py": "x\n" * 500},
            history=[Message(role="user", content="h" * 400)],
            user_input="u" * 400,
            memory="M" * 3000,
        )
        assert _goal_line(msgs) is not None  # the goal line is present…
        assert _content_tokens(msgs) <= _budget_ceiling(profile)  # …and still in budget


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


# -- project memory loading (IC-1003) ------------------------------------------


def _write_memory(ws: Path, text: str) -> Path:
    path = ws / "IRONCORE.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_project_memory_reads_file_verbatim(tmp_path):
    content = "## Verify\n- `pytest -q`\n"
    _write_memory(tmp_path, content)
    assert load_project_memory(tmp_path, profile=_profile()) == content


def test_load_project_memory_missing_file_returns_empty(tmp_path):
    assert load_project_memory(tmp_path, profile=_profile()) == ""


def test_load_project_memory_empty_file_returns_empty(tmp_path):
    _write_memory(tmp_path, "")
    assert load_project_memory(tmp_path, profile=_profile()) == ""


def test_load_project_memory_oversize_truncated_with_marker(tmp_path):
    profile = _profile(honest_context=400)  # memory budget = 40 tokens (~160 chars)
    _write_memory(tmp_path, "Z" * 4000)
    result = load_project_memory(tmp_path, profile=profile)
    budget = int(400 * SYSTEM_SHARE)
    assert MEMORY_MARKER in result  # honest marker on the truncated tail
    assert estimate_tokens(result) <= budget
    assert result.startswith("Z")  # the head of the file is what survives


def test_budget_ratio_controls_the_memory_budget(tmp_path):
    content = "line\n" * 60  # ~75 tokens
    _write_memory(tmp_path, content)
    profile = _profile(honest_context=4096)  # default budget ~409 tokens -> fits
    assert load_project_memory(tmp_path, profile=profile) == content
    small = load_project_memory(tmp_path, profile=profile, budget_ratio=0.01)  # 40 tokens
    assert small != content
    assert MEMORY_MARKER in small


def test_oversize_memory_summarized_once_and_cached(tmp_path):
    profile = _profile(honest_context=400)
    _write_memory(tmp_path, "Z" * 4000)
    calls: list[str] = []

    def summarizer(text: str) -> str:
        calls.append(text)
        return "SUMMARY: build with uv, verify with pytest -q"

    first = load_project_memory(tmp_path, profile=profile, summarizer=summarizer)
    second = load_project_memory(tmp_path, profile=profile, summarizer=summarizer)
    assert first == second == "SUMMARY: build with uv, verify with pytest -q"
    assert len(calls) == 1  # second load hit the (path, mtime, budget) cache


def test_summarizer_reruns_when_budget_key_differs(tmp_path):
    profile = _profile(honest_context=400)
    _write_memory(tmp_path, "Z" * 4000)  # oversize at both budgets below
    calls: list[str] = []

    def summarizer(text: str) -> str:
        calls.append(text)
        return "S"

    load_project_memory(tmp_path, profile=profile, summarizer=summarizer)
    load_project_memory(tmp_path, profile=profile, summarizer=summarizer)  # cache hit
    load_project_memory(tmp_path, profile=profile, budget_ratio=0.05, summarizer=summarizer)
    assert len(calls) == 2  # different budget -> different key -> re-summarized once


def test_summarizer_not_called_when_content_fits(tmp_path):
    _write_memory(tmp_path, "small note")
    calls: list[str] = []
    out = load_project_memory(
        tmp_path, profile=_profile(), summarizer=lambda t: calls.append(t) or "X"
    )
    assert out == "small note"
    assert calls == []  # in-budget content is returned verbatim, never summarized


# -- instruction-file compat + user-global memory (PKG-3) ----------------------
#
# A tmp `user_home` is injected everywhere so the loader never reads the real
# ~/.ironcore/IRONCORE.md (deterministic + hermetic on any machine).


def _empty_home(tmp_path: Path) -> Path:
    return tmp_path / "no-such-home"  # has no .ironcore/IRONCORE.md


def _write_user_global(home: Path, text: str) -> Path:
    d = home / ".ironcore"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "IRONCORE.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_agents_md_used_when_ironcore_absent(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agent notes\nBuild with uv.\n", encoding="utf-8")
    out = load_project_memory(tmp_path, profile=_profile(), user_home=_empty_home(tmp_path))
    assert out == "# Agent notes\nBuild with uv.\n"  # lone source -> verbatim, no label


def test_agents_md_fallback_without_home_injection(tmp_path):
    # No user_home injected: proves the DEFAULT path (Path.home()) reads AGENTS.md
    # when IRONCORE.md is absent. This one fails cleanly on pre-PKG-3 source
    # (which returned "" because it only ever read IRONCORE.md).
    (tmp_path / "AGENTS.md").write_text("agents fallback body\n", encoding="utf-8")
    out = load_project_memory(tmp_path, profile=_profile())
    assert "agents fallback body" in out


def test_claude_md_used_when_ironcore_and_agents_absent(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("claude notes\n", encoding="utf-8")
    out = load_project_memory(tmp_path, profile=_profile(), user_home=_empty_home(tmp_path))
    assert out == "claude notes\n"


def test_ironcore_md_wins_over_agents_and_claude(tmp_path):
    _write_memory(tmp_path, "native memory\n")
    (tmp_path / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude\n", encoding="utf-8")
    out = load_project_memory(tmp_path, profile=_profile(), user_home=_empty_home(tmp_path))
    assert out == "native memory\n"
    assert "agents" not in out and "claude" not in out


def test_agents_md_wins_over_claude_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents body\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude body\n", encoding="utf-8")
    out = load_project_memory(tmp_path, profile=_profile(), user_home=_empty_home(tmp_path))
    assert out == "agents body\n"
    assert "claude" not in out


def test_empty_agents_md_falls_through_to_claude_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("", encoding="utf-8")  # empty -> not a source
    (tmp_path / "CLAUDE.md").write_text("claude body\n", encoding="utf-8")
    out = load_project_memory(tmp_path, profile=_profile(), user_home=_empty_home(tmp_path))
    assert out == "claude body\n"


def test_user_global_alone_is_returned_verbatim(tmp_path):
    home = tmp_path / "home"
    _write_user_global(home, "GLOBAL: prefer ruff\n")
    out = load_project_memory(tmp_path, profile=_profile(), user_home=home)
    assert out == "GLOBAL: prefer ruff\n"  # single source -> verbatim, no label


def test_user_global_merged_before_project_memory(tmp_path):
    home = tmp_path / "home"
    _write_user_global(home, "GLOBAL: prefer ruff\n")
    _write_memory(tmp_path, "PROJECT: run pytest\n")
    out = load_project_memory(tmp_path, profile=_profile(), user_home=home)
    assert "GLOBAL: prefer ruff" in out
    assert "PROJECT: run pytest" in out
    # user-global precedes project in the composed body, and each is labelled
    assert out.index("GLOBAL: prefer ruff") < out.index("PROJECT: run pytest")
    assert "User-global memory" in out
    assert "Project memory (IRONCORE.md)" in out


def test_user_global_merges_with_agents_md_fallback(tmp_path):
    home = tmp_path / "home"
    _write_user_global(home, "GLOBAL body\n")
    (tmp_path / "AGENTS.md").write_text("AGENTS body\n", encoding="utf-8")
    out = load_project_memory(tmp_path, profile=_profile(), user_home=home)
    assert out.index("GLOBAL body") < out.index("AGENTS body")
    assert "Project memory (AGENTS.md)" in out  # provenance label names the real source


def test_combined_memory_truncates_within_budget(tmp_path):
    home = tmp_path / "home"
    _write_user_global(home, "U" * 8000)
    _write_memory(tmp_path, "P" * 8000)
    profile = _profile(honest_context=1000)  # SYSTEM share = 100 tokens
    out = load_project_memory(tmp_path, profile=profile, user_home=home)
    budget = int(1000 * SYSTEM_SHARE)
    assert estimate_tokens(out) <= budget  # the hard budget invariant holds
    assert MEMORY_MARKER in out  # honest marker shows a cap fired
    # both sources still represented (budget SPLIT, not tail-dropped): each
    # label survives AND at least some of each body's bytes made it in.
    assert "User-global memory" in out
    assert "Project memory (IRONCORE.md)" in out
    assert "U" in out and "P" in out


def test_combined_memory_tiny_window_never_exceeds_budget(tmp_path):
    # Even at a window too small for the provenance labels, the invariant holds
    # and nothing raises (honest degrade to a sliver, not a crash).
    home = tmp_path / "home"
    _write_user_global(home, "U" * 8000)
    _write_memory(tmp_path, "P" * 8000)
    for hc in (40, 120, 300, 400):
        profile = _profile(honest_context=hc)
        out = load_project_memory(tmp_path, profile=profile, user_home=home)
        assert estimate_tokens(out) <= int(hc * SYSTEM_SHARE)


def test_combined_memory_flows_through_compose_within_ceiling(tmp_path):
    home = tmp_path / "home"
    _write_user_global(home, "GLOBAL: prefer ruff\n")
    _write_memory(tmp_path, "PROJECT: run pytest\n")
    profile = _profile()
    memory = load_project_memory(tmp_path, profile=profile, user_home=home)
    msgs = _compose(SessionState(turn_count=0), profile, memory=memory)
    assert "GLOBAL: prefer ruff" in msgs[0].content
    assert "PROJECT: run pytest" in msgs[0].content
    assert _content_tokens(msgs) <= _budget_ceiling(profile)


def test_no_memory_sources_returns_empty(tmp_path):
    out = load_project_memory(tmp_path, profile=_profile(), user_home=_empty_home(tmp_path))
    assert out == ""


# -- compose() budgets a large memory into the SYSTEM share --------------------


def test_memory_text_appears_in_composed_system_context():
    msgs = _compose(SessionState(turn_count=0), _profile(), memory="RUN: uv run pytest -q")
    assert "RUN: uv run pytest -q" in msgs[0].content  # rides the system message
    assert "Project memory" in msgs[0].content  # under the MEMORY_HEADER label


def test_compose_caps_large_memory_into_system_share():
    profile = _profile(honest_context=400)  # system share = 40 tokens
    msgs = _compose(SessionState(turn_count=0), profile, memory="M" * 10000)
    system = msgs[0].content
    assert estimate_tokens(system) <= int(400 * SYSTEM_SHARE)  # capped, not naively glued
    assert MEMORY_MARKER in system  # honest marker shows the cap fired
    assert _content_tokens(msgs) <= _budget_ceiling(profile)


def test_budget_invariant_holds_with_large_memory():
    profiles = [_profile(256), _profile(1024), _profile(4096, 12), _profile(8192, 3)]
    for profile in profiles:
        for mem_len in (0, 500, 5000, 50000):
            msgs = _compose(
                SessionState(
                    goal="g" * 500,
                    plan_steps=["a" * 300, "b"],
                    plan_cursor=0,
                    turn_count=0,
                ),
                profile,
                working_set={"f.py": "x\n" * 500},
                history=[Message(role="user", content="h" * 400)],
                user_input="u" * 400,
                memory="M" * mem_len,
            )
            ceiling = _budget_ceiling(profile)
            assert _content_tokens(msgs) <= ceiling, (profile.honest_context, mem_len)


# -- model-aware tokenization (MS-1): measured chars_per_token ------------------


def _ratio_profile(honest_context: int = 4096, cpt: float = 4.0) -> CapabilityProfile:
    return CapabilityProfile(
        model_id="test-model", honest_context=honest_context, chars_per_token=cpt
    )


def test_estimate_tokens_divides_by_the_measured_ratio():
    assert estimate_tokens("abcdef", 2.0) == 3
    assert estimate_tokens("abcdef", 6.0) == 1
    assert estimate_tokens("abcdefg", 2.0) == 4  # ceil, never floor
    assert estimate_tokens("", 2.0) == 0


def test_estimate_tokens_default_ratio_is_byte_identical_legacy():
    for s in ("", "a", "abcd", "abcde", "x" * 400, "line\n" * 60):
        assert estimate_tokens(s, 4.0) == (len(s) + 3) // 4 == estimate_tokens(s)


def test_estimate_tokens_invalid_ratio_falls_back_to_default():
    for bad in (0.0, -1.0, float("inf"), float("-inf"), float("nan")):
        assert estimate_tokens("x" * 400, bad) == 100  # the guarded 4.0 fallback


def test_truncate_honors_a_non_default_ratio():
    from ironcore.core.composer import _truncate_to_tokens

    text = "z" * 5000
    for cpt in (1.0, 2.0, 3.3, 4.0, 8.0):
        out = _truncate_to_tokens(text, 100, FILE_MARKER, cpt)
        assert out  # room for content at every ratio here
        assert estimate_tokens(out, cpt) <= 100  # the invariant at THAT ratio
        assert FILE_MARKER in out


def test_smaller_ratio_packs_fewer_history_messages():
    history = [Message(role="user", content="z" * 200) for _ in range(8)]
    # history region = int(400 * 0.25) = 100 tokens
    legacy = _compose(
        SessionState(turn_count=3),  # off-cadence: no anchor noise
        _ratio_profile(400, 4.0),
        history=list(history),
        user_input="",
    )
    dense = _compose(
        SessionState(turn_count=3),
        _ratio_profile(400, 2.0),  # measured: chars are twice as expensive
        history=list(history),
        user_input="",
    )
    n_legacy = sum(1 for m in legacy if m.content.startswith("z"))
    n_dense = sum(1 for m in dense if m.content.startswith("z"))
    assert n_legacy == 2  # 50 tokens each at chars/4
    assert n_dense == 1  # 100 tokens each at chars/2
    assert n_dense < n_legacy


def test_budget_invariant_holds_at_measured_ratios():
    for cpt in (1.0, 2.0, 3.3, 5.5, 8.0):
        for hc in (256, 1024, 4096):
            profile = _ratio_profile(hc, cpt)
            msgs = _compose(
                SessionState(
                    goal="g" * 300,
                    plan_steps=["a" * 200, "b"],
                    plan_cursor=0,
                    turn_count=0,
                ),
                profile,
                working_set={"f.py": "x\n" * 500},
                history=[Message(role="user", content="h" * 400)],
                user_input="u" * 400,
                memory="M" * 3000,
            )
            used = sum(estimate_tokens(m.content, cpt) for m in msgs)
            ceiling = hc - int(hc * RESPONSE_HEADROOM_SHARE)
            assert used <= ceiling, (cpt, hc)


def test_default_profile_composition_is_byte_identical_to_legacy():
    # a profile that never met the TOKEN-RATIO probe carries 4.0 -> identical output
    state = SessionState(turn_count=0, goal="ship MS-1")
    kw = dict(
        working_set={"a.py": "print('a')\n" * 30},
        history=[Message(role="assistant", content="earlier reply " * 40)],
        user_input="do the thing",
        memory="note " * 50,
    )
    # compose is pure and never mutates inputs (pinned above), so kw is reusable
    legacy = _compose(state, _profile(1024), **kw)
    explicit = _compose(state, _ratio_profile(1024, 4.0), **kw)
    assert legacy == explicit


def test_load_project_memory_uses_the_profile_ratio(tmp_path):
    _write_memory(tmp_path, "Z" * 4000)
    # honest_context 400 -> memory budget 40 tokens; at cpt=2.0 that's ~80 chars
    dense = load_project_memory(tmp_path, profile=_ratio_profile(400, 2.0))
    legacy = load_project_memory(tmp_path, profile=_ratio_profile(400, 4.0))
    assert MEMORY_MARKER in dense and MEMORY_MARKER in legacy
    assert estimate_tokens(dense, 2.0) <= 40
    assert estimate_tokens(legacy, 4.0) <= 40
    assert len(dense) < len(legacy)  # a denser tokenizer keeps fewer chars


# -- image budgeting (MS-6): flat charge, keep-newest cap, honest drop ----------


def _img_msg(text: str, n: int = 1) -> Message:
    from ironcore.providers.base import ImageData

    return Message(
        role="user", content=text, images=[ImageData(base64="QUJD") for _ in range(n)]
    )


def test_history_keeps_images_only_on_the_newest_two():
    from ironcore.core.composer import IMAGE_DROPPED_MARKER, _select_history

    history = [_img_msg("first"), _img_msg("second"), _img_msg("third")]
    out = _select_history(history, budget=4096)
    assert [len(m.images) for m in out] == [0, 1, 1]  # newest 2 keep their image
    assert IMAGE_DROPPED_MARKER in out[0].content  # oldest: honest strip
    assert "first" in out[0].content  # the text survives the strip
    assert IMAGE_DROPPED_MARKER not in out[1].content


def test_image_charge_drops_a_message_the_text_alone_would_fit():
    from ironcore.core.composer import IMAGE_TOKEN_COST, _select_history

    msg = _img_msg("tiny")
    # text costs 1 token; the image charge alone exceeds the region
    out = _select_history([msg], budget=IMAGE_TOKEN_COST - 1)
    assert out == []
    # without an image the same text fits fine
    assert _select_history([Message(role="user", content="tiny")], budget=8) != []


def test_kept_images_are_charged_against_the_history_budget():
    from ironcore.core.composer import IMAGE_TOKEN_COST, _select_history

    filler = [Message(role="user", content="h" * 400) for _ in range(4)]  # 100 tokens each
    history = [*filler, _img_msg("shot")]
    budget = IMAGE_TOKEN_COST + 250
    out = _select_history(history, budget=budget)
    # the newest (image) message costs ~513; only two 100-token fillers still fit
    assert len(out) == 3
    assert out[-1].images and "shot" in out[-1].content


def test_compose_budget_invariant_holds_with_images():
    profile = _profile(4096)
    history = [_img_msg(f"screenshot {i} " * 20) for i in range(5)]
    msgs = _compose(
        SessionState(turn_count=0, goal="look at the shots"),
        profile,
        working_set={"a.py": "print('a')\n" * 40},
        history=history,
        user_input="compare them",
    )
    assert _content_tokens(msgs) <= _budget_ceiling(profile)
    assert sum(len(m.images) for m in msgs) <= 2  # the keep-newest cap held
