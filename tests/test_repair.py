"""LadderRepairPolicy + frame_error (IC-503, SPEC §5.4).

Two layers of coverage:

* the pinned decision table and ``frame_error`` framing as pure-function units;
* an ENGINE-INTEGRATION layer that drives the real ``TurnEngine`` with a scripted
  ``MockProvider`` and ``repair=LadderRepairPolicy()`` (mirroring
  ``tests/test_engine.py``): a malformed-then-valid script is repaired and
  executes; a malformed-native-twice script ladders down to the text floor and
  a valid IRONCALL call then runs; a malformed-at-floor-twice script gives up
  with ``stop_reason="error"``. Zero network, zero model; ``asyncio.run`` drives
  the async loop.
"""

from __future__ import annotations

import asyncio

from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.core.events import (
    TextDelta,
    ToolCallFinished,
    TurnCompleted,
)
from ironcore.core.protocols import RepairAction
from ironcore.core.repair import (
    TEXT_PROTOCOL_FLOOR,
    LadderRepairPolicy,
    frame_error,
)
from ironcore.core.state import SessionState
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MalformedToolJSON, MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry

# --------------------------------------------------------------------------- #
# (1) decision table — the pinned contract
# --------------------------------------------------------------------------- #


def test_first_failure_retries_at_every_rung():
    policy = LadderRepairPolicy()
    for rung in ("native", "strict_json", TEXT_PROTOCOL_FLOOR):
        assert policy.decide(attempt=0, error="e", raw="r", rung=rung) == RepairAction.RETRY


def test_second_failure_ladders_down_above_the_floor():
    policy = LadderRepairPolicy()
    assert policy.decide(attempt=1, error="e", raw="r", rung="native") == RepairAction.LADDER_DOWN
    assert (
        policy.decide(attempt=1, error="e", raw="r", rung="strict_json")
        == RepairAction.LADDER_DOWN
    )


def test_second_failure_at_the_text_floor_gives_up():
    policy = LadderRepairPolicy()
    assert (
        policy.decide(attempt=1, error="e", raw="r", rung=TEXT_PROTOCOL_FLOOR)
        == RepairAction.GIVE_UP
    )


def test_over_max_attempts_gives_up_regardless_of_rung():
    policy = LadderRepairPolicy(max_attempts=4)
    # The cap dominates even a rung that would otherwise ladder down.
    assert policy.decide(attempt=4, error="e", raw="r", rung="native") == RepairAction.GIVE_UP
    assert policy.decide(attempt=9, error="e", raw="r", rung="native") == RepairAction.GIVE_UP


def test_custom_max_attempts_bounds_the_loop():
    policy = LadderRepairPolicy(max_attempts=1)
    assert policy.decide(attempt=0, error="e", raw="r", rung="native") == RepairAction.RETRY
    # attempt 1 == max_attempts -> cap fires before ladder-down could.
    assert policy.decide(attempt=1, error="e", raw="r", rung="native") == RepairAction.GIVE_UP


def test_decide_is_pure_and_deterministic():
    policy = LadderRepairPolicy()
    a = policy.decide(attempt=1, error="x", raw="y", rung="native")
    b = policy.decide(attempt=1, error="x", raw="y", rung="native")
    assert a == b == RepairAction.LADDER_DOWN


# --------------------------------------------------------------------------- #
# (2) frame_error — actionable, rung-aware feedback
# --------------------------------------------------------------------------- #


def test_frame_error_names_the_floor_format():
    msg = frame_error("bad JSON at col 3", '{"tool": broken', TEXT_PROTOCOL_FLOOR)
    assert "bad JSON at col 3" in msg  # what went wrong
    assert '{"tool": broken' in msg  # the offending text
    assert "ironcall" in msg  # how to fix, for THIS rung's format


def test_frame_error_names_the_native_format():
    msg = frame_error("arguments did not parse", "{...", "native")
    assert "native function-calling" in msg
    assert "arguments did not parse" in msg


def test_frame_error_survives_empty_inputs():
    msg = frame_error("", "", "native")
    assert msg  # non-empty
    assert "native function-calling" in msg


def test_frame_error_bounds_a_huge_raw():
    huge = "x" * 5000
    msg = frame_error("truncated", huge, TEXT_PROTOCOL_FLOOR)
    assert "more chars omitted" in msg
    assert len(msg) < 1200  # bounded, not the full 5k


def test_frame_error_unknown_rung_is_still_actionable():
    msg = frame_error("nope", "raw", "some_future_rung")
    assert "some_future_rung" in msg
    assert "re-issue" in msg


# --------------------------------------------------------------------------- #
# engine-integration helpers (mirrors tests/test_engine.py)
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


def _engine(tmp_path, script, *, protocol: str = "native") -> TurnEngine:
    settings = Settings.model_validate({"safety": {"network_tools": False}})
    tools = build_default_registry(settings, tmp_path)
    return TurnEngine(
        MockProvider(list(script)),
        tools,
        settings,
        _profile(protocol),
        Mode.MANUAL,
        workspace=tmp_path,
        repair=LadderRepairPolicy(),
        snapshots=None,
        session=SessionState(),
    )


def _drive(engine: TurnEngine, user_input: str) -> list:
    events: list = []

    async def _run():
        async for ev in engine.run_turn(user_input):
            events.append(ev)

    asyncio.run(_run())
    return events


def _of(events, cls) -> list:
    return [e for e in events if isinstance(e, cls)]


def _text_of(events) -> str:
    return "".join(e.text for e in _of(events, TextDelta))


# --------------------------------------------------------------------------- #
# (3) INTEGRATION: malformed then valid -> repaired, the valid call executes
# --------------------------------------------------------------------------- #


def test_engine_repairs_malformed_then_executes_valid_call(tmp_path):
    (tmp_path / "hello.txt").write_text("hi there\n", encoding="utf-8")
    script = [
        MalformedToolJSON(text_prefix="let me read it"),  # 1st CALL: RETRY
        _text("", [_call("read_file", {"path": "hello.txt"})]),  # re-ask: valid
        _text("read it, done"),  # model stops
    ]
    events = _drive(_engine(tmp_path, script, protocol="native"), "read hello.txt")

    fins = _of(events, ToolCallFinished)
    assert len(fins) == 1  # the repaired-into valid call actually ran
    assert fins[0].call.name == "read_file"
    assert fins[0].result.ok and "hi there" in fins[0].result.output
    assert "[repair]" in _text_of(events)  # visible, never silent
    assert isinstance(events[-1], TurnCompleted) and events[-1].stop_reason == "done"


# --------------------------------------------------------------------------- #
# (4) INTEGRATION: malformed native twice -> LADDER_DOWN, floor call then runs
# --------------------------------------------------------------------------- #


def test_engine_ladders_down_to_floor_then_executes(tmp_path):
    (tmp_path / "hello.txt").write_text("floor works\n", encoding="utf-8")
    block = '```ironcall\n{"tool": "read_file", "args": {"path": "hello.txt"}}\n```'
    script = [
        MalformedToolJSON(text_prefix="try one"),  # (0, native) -> RETRY
        MalformedToolJSON(text_prefix="try two"),  # (1, native) -> LADDER_DOWN
        _text(f"now on the floor\n{block}"),  # parsed via IRONCALL at the floor
        _text("all done"),  # model stops
    ]
    events = _drive(_engine(tmp_path, script, protocol="native"), "read it")

    fins = _of(events, ToolCallFinished)
    assert len(fins) == 1  # ladder-down reached a working floor and executed
    assert fins[0].call.name == "read_file"
    assert "floor works" in fins[0].result.output
    assert _text_of(events).count("[repair]") >= 2  # both failures surfaced
    assert isinstance(events[-1], TurnCompleted) and events[-1].stop_reason == "done"


# --------------------------------------------------------------------------- #
# (5) INTEGRATION: malformed at the floor twice -> GIVE_UP, stop_reason error
# --------------------------------------------------------------------------- #


def test_engine_gives_up_on_two_floor_failures(tmp_path):
    script = [
        MalformedToolJSON(text_prefix="floor try one"),  # (0, floor) -> RETRY
        MalformedToolJSON(text_prefix="floor try two"),  # (1, floor) -> GIVE_UP
    ]
    events = _drive(_engine(tmp_path, script, protocol="text_protocol"), "use a tool")

    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].stop_reason == "error"
    assert not _of(events, ToolCallFinished)  # nothing ever executed
    assert "[repair]" in _text_of(events)
