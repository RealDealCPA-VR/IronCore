"""Budget (IC-506): per-turn/-session caps + runaway loop detection (SPEC §5.6).

Unit cases exercise every cap through a fully deterministic INJECTED clock (no
real time, no sleeps); the final case is an ENGINE-INTEGRATION test that drives
the real ``TurnEngine`` state machine with a scripted ``MockProvider`` and asserts
a low provider-call cap ends the turn with ``stop_reason == "budget"``. Async is
driven with ``asyncio.run`` (no pytest-asyncio), mirroring ``tests/test_engine.py``.
"""

from __future__ import annotations

import asyncio

from ironcore.config.settings import Settings
from ironcore.core.budgets import Budget
from ironcore.core.engine import TurnEngine
from ironcore.core.events import ToolCallFinished, TurnCompleted
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry

# --------------------------------------------------------------------------- #
# turn caps
# --------------------------------------------------------------------------- #


def test_max_calls_cap_trips_check():
    budget = Budget(max_provider_calls=3)
    budget.start_turn()
    assert budget.check() is None
    budget.record_call(1)
    budget.record_call(1)
    assert budget.check() is None  # 2 < 3
    budget.record_call(1)
    assert budget.check() == "budget"  # 3rd call reaches the cap


def test_max_tokens_cap_trips_check():
    budget = Budget(max_tokens=100, max_provider_calls=999)
    budget.start_turn()
    budget.record_call(60)
    assert budget.check() is None
    budget.record_call(40)  # cumulative 100 >= 100
    assert budget.check() == "budget"


def test_wall_clock_cap_trips_with_injected_clock():
    now = [100.0]
    budget = Budget(max_seconds=5.0, max_provider_calls=999, clock=lambda: now[0])
    budget.start_turn()  # turn_start captured at 100.0
    assert budget.check() is None
    now[0] = 104.9
    assert budget.check() is None  # 4.9s < 5s
    now[0] = 105.0
    assert budget.check() == "budget"  # elapsed 5.0s >= 5s


def test_repair_cap_trips():
    budget = Budget(max_repairs=2, max_provider_calls=999)
    budget.start_turn()
    assert budget.note_repair() is None  # 1st attempt
    assert budget.check() is None
    assert budget.note_repair() == "budget"  # 2nd attempt reaches the cap
    assert budget.check() == "budget"


# --------------------------------------------------------------------------- #
# loop detection (2× warn / 3× stop)
# --------------------------------------------------------------------------- #


def test_loop_detector_third_identical_stops():
    budget = Budget()
    budget.start_turn()
    assert budget.note_tool("grep", {"pattern": "x"}) is None  # 1st
    assert budget.note_tool("grep", {"pattern": "x"}) is None  # 2nd = intervention
    assert budget.note_tool("grep", {"pattern": "x"}) == "budget"  # 3rd = stop
    assert budget.summary()["interventions"] == 1


def test_loop_detector_resets_on_different_call():
    budget = Budget()
    budget.start_turn()
    assert budget.note_tool("grep", {"pattern": "x"}) is None
    assert budget.note_tool("grep", {"pattern": "x"}) is None
    # different args break the streak — back to a fresh count
    assert budget.note_tool("grep", {"pattern": "y"}) is None
    assert budget.note_tool("grep", {"pattern": "y"}) is None
    assert budget.note_tool("grep", {"pattern": "y"}) == "budget"


def test_note_tool_canonicalizes_arg_order():
    budget = Budget()
    budget.start_turn()
    assert budget.note_tool("edit", {"a": 1, "b": 2}) is None
    assert budget.note_tool("edit", {"b": 2, "a": 1}) is None  # same call, keys reordered
    assert budget.note_tool("edit", {"a": 1, "b": 2}) == "budget"


def test_note_tool_never_raises_on_unserializable_args():
    budget = Budget()
    budget.start_turn()
    weird = {"callback": lambda: 1}  # not JSON-serializable
    assert budget.note_tool("t", weird) is None
    assert budget.note_tool("t", weird) is None
    assert budget.note_tool("t", weird) == "budget"


# --------------------------------------------------------------------------- #
# should_continue / start_turn / summary
# --------------------------------------------------------------------------- #


def test_should_continue_flips_false_when_cap_hit():
    budget = Budget(max_provider_calls=1)
    budget.start_turn()
    assert budget.should_continue() is True
    budget.record_call(0)
    assert budget.should_continue() is False


def test_start_turn_resets_turn_but_not_session_totals():
    budget = Budget()
    budget.start_turn()
    budget.record_call(50)
    budget.record_call(50)
    first = budget.summary()
    assert first["calls"] == 2 and first["tokens"] == 100
    assert first["session_calls"] == 2 and first["session_tokens"] == 100

    budget.start_turn()  # new turn: per-turn zeroed, session preserved
    after = budget.summary()
    assert after["calls"] == 0 and after["tokens"] == 0
    assert after["session_calls"] == 2 and after["session_tokens"] == 100


def test_summary_reports_spent():
    now = [0.0]
    budget = Budget(clock=lambda: now[0])
    budget.start_turn()
    budget.record_call(30)
    now[0] = 2.5
    spent = budget.summary()
    assert spent["calls"] == 1
    assert spent["tokens"] == 30
    assert spent["elapsed"] == 2.5


# --------------------------------------------------------------------------- #
# session caps + from_settings
# --------------------------------------------------------------------------- #


def test_session_caps_persist_across_turns():
    budget = Budget(max_provider_calls=999, max_session_calls=3)
    budget.start_turn()
    budget.record_call(1)
    budget.record_call(1)
    budget.start_turn()  # resets the turn cap, NOT the session count
    assert budget.check() is None  # session total 2 < 3
    budget.record_call(1)  # session total 3
    assert budget.check() == "budget"


def test_session_token_cap_trips():
    budget = Budget(max_provider_calls=999, max_session_tokens=100)
    budget.start_turn()
    budget.record_call(80)
    budget.start_turn()
    assert budget.check() is None
    budget.record_call(20)  # session tokens 100 >= 100
    assert budget.check() == "budget"


def test_from_settings_uses_defaults_without_budget_section():
    settings = Settings.model_validate({})
    budget = Budget.from_settings(settings)
    assert budget.max_provider_calls == 20
    assert budget.max_session_calls is None
    budget.start_turn()
    assert budget.should_continue() is True


# --------------------------------------------------------------------------- #
# engine integration: a low call cap ends the turn with stop_reason "budget"
# --------------------------------------------------------------------------- #


def _profile(protocol: str = "native") -> CapabilityProfile:
    tp = {"native": 1.0} if protocol == "native" else {}
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols=tp)


def _text(content: str, calls: list[ToolCall] | None = None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=calls or [])
    )


def _call(name: str, args: dict, cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def _drive(engine: TurnEngine, user_input: str) -> list:
    events: list = []

    async def _run():
        async for ev in engine.run_turn(user_input):
            events.append(ev)

    asyncio.run(_run())
    return events


def test_engine_stops_turn_on_provider_call_budget(tmp_path):
    (tmp_path / "loop.txt").write_text("x\n", encoding="utf-8")
    settings = Settings.model_validate({})
    tools = build_default_registry(settings, tmp_path)
    # the model keeps asking to read the same file; the call cap must stop it.
    same = _text("", [_call("read_file", {"path": "loop.txt"})])
    engine = TurnEngine(
        MockProvider([same, same, same, same]),
        tools,
        settings,
        _profile("native"),
        Mode.MANUAL,
        workspace=tmp_path,
        budget=Budget(max_provider_calls=2),
        snapshots=None,
    )
    events = _drive(engine, "keep reading")

    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed and completed[-1].stop_reason == "budget"
    # check() trips before the 3rd provider call, so exactly 2 reads executed.
    finishes = [e for e in events if isinstance(e, ToolCallFinished)]
    assert len(finishes) == 2
