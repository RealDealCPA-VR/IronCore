"""End-to-end proof of outcome for phases 5 (turn engine) and 6 (envelope).

The rest of the suite unit-tests each module. This drives the REAL TurnEngine
through complete turns against a scripted MockProvider on a real workspace —
proving the state machine gates every tool, repairs malformed output, stops
runaways, honors PLAN/approvals, redacts secrets before they reach the model,
runs the IRONCALL floor protocol — and then proves the envelope's
measure -> profile -> adapt loop feeds a real profile back into the engine.
No real model, no network; every outcome is observed, not asserted on faith.
"""

import asyncio

from ironcore.config.settings import Settings
from ironcore.core.approvals import ApprovalAnswer, ApprovalBroker
from ironcore.core.budgets import Budget
from ironcore.core.engine import TurnEngine
from ironcore.core.events import (
    ApprovalRequired,
    ToolCallFinished,
    ToolCallRequested,
    TurnCompleted,
)
from ironcore.envelope.probe_tools import ToolFormProbe
from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.runner import run_probes
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools import build_default_registry

WEATHER_ARGS = {"city": "Paris", "units": "celsius"}


def _native_profile() -> CapabilityProfile:
    return CapabilityProfile(
        model_id="mock", honest_context=8192, tool_protocols={"native": 1.0}
    )


def _assistant(content="", calls=None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=calls or [])
    )


def _drive(engine, user_input):
    events = []

    async def go():
        async for ev in engine.run_turn(user_input):
            events.append(ev)

    asyncio.run(go())
    return events


def _final(events) -> TurnCompleted:
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed, "turn did not complete"
    return completed[-1]


# --------------------------------------------------------------------------- #
# 1. A full agent turn: read -> edit -> stop, every tool gated, work done
# --------------------------------------------------------------------------- #


def test_full_native_turn_reads_then_edits_then_completes(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", newline="")
    registry = build_default_registry(Settings(), tmp_path)

    read = ToolCall(id="c1", name="read_file", arguments={"path": "app.py"})
    edit = ToolCall(
        id="c2",
        name="edit_file",
        arguments={
            "path": "app.py",
            "format": "search_replace",
            "edit": "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n",
        },
    )
    provider = MockProvider(
        [_assistant(calls=[read]), _assistant(calls=[edit]), _assistant("done")]
    )
    engine = TurnEngine(
        provider, registry, Settings(), _native_profile(), Mode.ACCEPT_EDITS,
        workspace=tmp_path, snapshots=None,
    )

    events = _drive(engine, "bump x to 2")

    executed = [e for e in events if isinstance(e, ToolCallFinished)]
    assert [e.call.name for e in executed] == ["read_file", "edit_file"]
    assert all(e.decision == "allow" for e in events if isinstance(e, ToolCallRequested))
    assert (tmp_path / "app.py").read_text() == "x = 2\n"  # the edit really applied
    assert _final(events).stop_reason == "done"  # evidence-based


# --------------------------------------------------------------------------- #
# 2. Safety: PLAN denies writes; MANUAL asks then executes on approval
# --------------------------------------------------------------------------- #


def test_plan_mode_denies_a_write_and_nothing_is_written(tmp_path):
    registry = build_default_registry(Settings(), tmp_path)
    write = ToolCall(id="c1", name="write_file", arguments={"path": "x.txt", "content": "hi"})
    provider = MockProvider([_assistant(calls=[write]), _assistant("ok, cannot")])
    engine = TurnEngine(
        provider, registry, Settings(), _native_profile(), Mode.PLAN,
        workspace=tmp_path, snapshots=None,
    )

    events = _drive(engine, "write a file")

    gated = [e for e in events if isinstance(e, ToolCallRequested)]
    assert gated and gated[0].decision == "deny"
    assert not any(isinstance(e, ToolCallFinished) for e in events)  # never executed
    assert not (tmp_path / "x.txt").exists()
    assert _final(events).stop_reason == "denied"


def test_manual_mode_asks_and_executes_on_approval(tmp_path):
    registry = build_default_registry(Settings(), tmp_path)
    write = ToolCall(id="c1", name="write_file", arguments={"path": "y.txt", "content": "hi"})
    provider = MockProvider([_assistant(calls=[write]), _assistant("wrote it")])

    holder: dict = {}

    async def on_request(req):  # a front end that approves
        holder["broker"].answer(req.id, ApprovalAnswer(decision="approve"))

    broker = ApprovalBroker(on_request=on_request, timeout=5.0)
    holder["broker"] = broker

    engine = TurnEngine(
        provider, registry, Settings(), _native_profile(), Mode.MANUAL,
        workspace=tmp_path, approvals=broker, snapshots=None,
    )

    events = _drive(engine, "write y.txt")

    assert any(isinstance(e, ApprovalRequired) for e in events)  # it asked
    assert any(isinstance(e, ToolCallFinished) for e in events)  # then executed
    assert (tmp_path / "y.txt").read_text() == "hi"


def test_auto_mode_still_denies_a_destructive_command(tmp_path):
    registry = build_default_registry(Settings(), tmp_path)
    danger = ToolCall(id="c1", name="shell", arguments={"command": "rm -rf /"})
    provider = MockProvider([_assistant(calls=[danger]), _assistant("blocked")])
    engine = TurnEngine(
        provider, registry, Settings(), _native_profile(), Mode.AUTO,
        workspace=tmp_path, snapshots=None,
    )

    events = _drive(engine, "clean up")

    gated = [e for e in events if isinstance(e, ToolCallRequested)]
    assert gated and gated[0].decision == "deny"  # command policy bites even in AUTO
    assert not any(isinstance(e, ToolCallFinished) for e in events)


# --------------------------------------------------------------------------- #
# 3. Runaway protection: the budget stops an infinite tool loop
# --------------------------------------------------------------------------- #


def test_budget_stops_a_runaway_loop(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", newline="")
    registry = build_default_registry(Settings(), tmp_path)
    read = ToolCall(id="c1", name="read_file", arguments={"path": "app.py"})
    # a model that keeps reading forever; the engine must cut it off
    provider = MockProvider([_assistant(calls=[read]) for _ in range(10)])
    engine = TurnEngine(
        provider, registry, Settings(), _native_profile(), Mode.AUTO,
        workspace=tmp_path, budget=Budget(max_provider_calls=3), snapshots=None,
    )

    events = _drive(engine, "read it")
    assert _final(events).stop_reason == "budget"
    assert len([e for e in events if isinstance(e, ToolCallFinished)]) <= 3


# --------------------------------------------------------------------------- #
# 4. Repair: malformed tool output is repaired, not fatal
# --------------------------------------------------------------------------- #


def test_malformed_tool_output_is_repaired_then_executes(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", newline="")
    registry = build_default_registry(Settings(), tmp_path)
    read = ToolCall(id="c1", name="read_file", arguments={"path": "app.py"})
    # native tool-call arguments that don't parse -> repairable, then a valid call
    from ironcore.providers.mock import MalformedToolJSON

    provider = MockProvider(
        [MalformedToolJSON(raw_fragment='{"broken'), _assistant(calls=[read]), _assistant("done")]
    )
    engine = TurnEngine(
        provider, registry, Settings(), _native_profile(), Mode.AUTO,
        workspace=tmp_path, snapshots=None,
    )

    events = _drive(engine, "read app.py")
    text = "".join(getattr(e, "text", "") for e in events)
    assert "[repair]" in text  # the repair was visible, never silent
    assert any(isinstance(e, ToolCallFinished) for e in events)  # recovered + executed
    assert _final(events).stop_reason == "done"


# --------------------------------------------------------------------------- #
# 5. The IRONCALL floor protocol runs a tool for a weak (unprobed) model
# --------------------------------------------------------------------------- #


def test_ironcall_text_protocol_executes_a_tool(tmp_path):
    (tmp_path / "app.py").write_text("hello\n", newline="")
    registry = build_default_registry(Settings(), tmp_path)
    # empty tool_protocols -> recommended protocol is the text_protocol floor
    profile = CapabilityProfile(model_id="weak", honest_context=8192)
    assert profile.recommended_tool_protocol() == "text_protocol"

    block = '```ironcall\n{"tool": "read_file", "args": {"path": "app.py"}}\n```'
    provider = MockProvider([_assistant(block), _assistant("that is the file")])
    engine = TurnEngine(
        provider, registry, Settings(), profile, Mode.AUTO,
        workspace=tmp_path, snapshots=None,
    )

    events = _drive(engine, "show app.py")
    executed = [e for e in events if isinstance(e, ToolCallFinished)]
    assert [e.call.name for e in executed] == ["read_file"]  # parsed from the text block
    assert _final(events).stop_reason == "done"


# --------------------------------------------------------------------------- #
# 6. Secrets in tool output are redacted before reaching the model
# --------------------------------------------------------------------------- #


def test_secret_in_a_read_file_is_redacted_before_the_next_call(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    (tmp_path / "config.py").write_text(f'API_KEY = "{secret}"\n', newline="")
    registry = build_default_registry(Settings(), tmp_path)
    read = ToolCall(id="c1", name="read_file", arguments={"path": "config.py"})
    provider = MockProvider([_assistant(calls=[read]), _assistant("got it")])
    engine = TurnEngine(
        provider, registry, Settings(), _native_profile(), Mode.AUTO,
        workspace=tmp_path, snapshots=None,
    )

    _drive(engine, "read the config")

    # the model's SECOND call carries the file content in history — it must be redacted
    later_calls = provider.calls[1:]
    assert later_calls, "expected a follow-up provider call carrying the tool result"
    blob = "".join(m.content for call in later_calls for m in call)
    assert secret not in blob  # the key never reached the model
    assert "[redacted:openai-key]" in blob  # and it was replaced honestly


# --------------------------------------------------------------------------- #
# 7. The envelope measure -> profile -> adapt loop, feeding the engine
# --------------------------------------------------------------------------- #


def _toolform_script(*, native_ok: bool, strict_ok: bool, text_ok: bool) -> list:
    """One completion per protocol trial, in the probe's fixed order."""
    native = (
        _assistant(calls=[ToolCall(id="n", name="get_weather", arguments=WEATHER_ARGS)])
        if native_ok
        else _assistant("i cannot call tools")
    )
    strict = _assistant(
        '{"tool": "get_weather", "args": {"city": "Paris", "units": "celsius"}}'
        if strict_ok
        else "no json here"
    )
    text = _assistant(
        '```ironcall\n{"tool": "get_weather", "args": {"city": "Paris", "units": "celsius"}}\n```'
        if text_ok
        else "plain prose"
    )
    return [native, strict, text]


def test_probes_measure_a_capable_model_and_the_engine_adopts_native(tmp_path):
    provider = MockProvider(_toolform_script(native_ok=True, strict_ok=True, text_ok=True))
    profile = asyncio.run(
        run_probes(
            provider,
            [ToolFormProbe(trials=1)],
            model_id="capable",
            probed_at="2026-07-16T00:00:00Z",
        )
    )
    # measured: every protocol reliable -> the ladder picks the most efficient
    assert profile.tool_protocols["native"] == 1.0
    assert profile.recommended_tool_protocol() == "native"

    # and that measured profile drives the engine down the native path
    (tmp_path / "app.py").write_text("hello\n", newline="")
    registry = build_default_registry(Settings(), tmp_path)
    read = ToolCall(id="c1", name="read_file", arguments={"path": "app.py"})
    eng_provider = MockProvider([_assistant(calls=[read]), _assistant("done")])
    engine = TurnEngine(
        eng_provider, registry, Settings(), profile, Mode.AUTO,
        workspace=tmp_path, snapshots=None,
    )
    events = _drive(engine, "read it")
    assert any(isinstance(e, ToolCallFinished) for e in events)


def test_probes_measure_a_weak_model_and_the_ladder_falls_to_the_floor():
    provider = MockProvider(_toolform_script(native_ok=False, strict_ok=False, text_ok=False))
    profile = asyncio.run(
        run_probes(
            provider, [ToolFormProbe(trials=1)], model_id="weak", probed_at="2026-07-16T00:00:00Z"
        )
    )
    assert profile.tool_protocols["native"] == 0.0
    # nothing clears its threshold -> the always-works text floor
    assert profile.recommended_tool_protocol() == "text_protocol"
