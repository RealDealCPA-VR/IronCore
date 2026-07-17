"""MCP tools through the REAL turn engine: the safety gate is unchanged.

A duck-typed fake MCP client backs an ``MCPTool`` registered alongside the
default registry, and a scripted ``MockProvider`` calls it. These tests pin
the load-bearing invariants: `mcp__*` tools gate as NET (MANUAL asks, PLAN
denies, AUTO still asks — never auto-allowed), and OBSERVE wraps the server's
output in UNTRUSTED markers so a hostile MCP server hits the same injection
fence every tool does.
"""

from __future__ import annotations

import asyncio

from ironcore.config.settings import Settings
from ironcore.core.approvals import ApprovalAnswer, ApprovalBroker
from ironcore.core.engine import TurnEngine
from ironcore.core.events import (
    ApprovalRequired,
    ToolCallFinished,
    ToolCallRequested,
    TurnCompleted,
)
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry
from ironcore.tools.mcp import MCPTool


class FakeClient:
    def __init__(self, result):
        self.server = "fake"
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


def _text(content: str, calls: list[ToolCall] | None = None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=calls or [])
    )


def _echo_call(cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name="mcp__fake__echo", arguments={"text": "hi"})


def _engine(tmp_path, script, *, mode: Mode, broker: ApprovalBroker | None = None):
    settings = Settings.model_validate({"safety": {"network_tools": True}})
    tools = build_default_registry(settings, tmp_path)
    client = FakeClient({"content": [{"type": "text", "text": "echo: hi"}]})
    tools.register(
        MCPTool(
            client=client,
            server="fake",
            remote_name="echo",
            description="Echo text back.",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        )
    )
    engine = TurnEngine(
        MockProvider(list(script)),
        tools,
        settings,
        _profile(),
        mode,
        workspace=tmp_path,
        approvals=broker,
        snapshots=None,
    )
    return engine, client


def _answering_broker(decision: str) -> ApprovalBroker:
    broker = ApprovalBroker(timeout=5.0)

    async def _on_request(req):
        broker.answer(req.id, ApprovalAnswer(decision=decision))

    broker.on_request = _on_request
    return broker


def drive(engine: TurnEngine, user_input: str) -> list:
    events: list = []

    async def _run():
        async for ev in engine.run_turn(user_input):
            events.append(ev)

    asyncio.run(_run())
    return events


def _of(events, cls) -> list:
    return [e for e in events if isinstance(e, cls)]


# --------------------------------------------------------------------------- #
# gate decisions per mode
# --------------------------------------------------------------------------- #


def test_manual_asks_with_net_risk_and_executes_on_approve(tmp_path):
    script = [_text("", [_echo_call()]), _text("done")]
    engine, client = _engine(
        tmp_path, script, mode=Mode.MANUAL, broker=_answering_broker("approve")
    )
    events = drive(engine, "use the mcp echo")

    req = _of(events, ToolCallRequested)[0]
    assert req.risk == "net" and req.decision == "ask"
    assert len(_of(events, ApprovalRequired)) == 1
    fins = _of(events, ToolCallFinished)
    assert len(fins) == 1 and fins[0].result.ok
    assert "echo: hi" in fins[0].result.output
    assert client.calls == [("echo", {"text": "hi"})]  # the server really ran
    assert events[-1].stop_reason == "done"


def test_manual_deny_blocks_the_server_call(tmp_path):
    script = [_text("", [_echo_call()]), _text("understood")]
    engine, client = _engine(tmp_path, script, mode=Mode.MANUAL, broker=_answering_broker("deny"))
    events = drive(engine, "use the mcp echo")

    assert not _of(events, ToolCallFinished)
    assert client.calls == []  # denied -> the subprocess-side tool never ran
    assert events[-1].stop_reason == "denied"


def test_plan_mode_denies_outright(tmp_path):
    script = [_text("", [_echo_call()]), _text("proposing only")]
    engine, client = _engine(tmp_path, script, mode=Mode.PLAN)
    events = drive(engine, "use the mcp echo")

    req = _of(events, ToolCallRequested)[0]
    assert req.risk == "net" and req.decision == "deny"
    assert not _of(events, ApprovalRequired)  # DENY, not ASK
    assert not _of(events, ToolCallFinished)
    assert client.calls == []
    assert events[-1].stop_reason == "denied"


def test_auto_mode_still_asks_net_never_auto_allowed(tmp_path):
    script = [_text("", [_echo_call()]), _text("ok, stopping")]
    engine, client = _engine(tmp_path, script, mode=Mode.AUTO, broker=_answering_broker("deny"))
    events = drive(engine, "use the mcp echo")

    req = _of(events, ToolCallRequested)[0]
    assert req.risk == "net" and req.decision == "ask"  # the frozen POLICY row
    assert len(_of(events, ApprovalRequired)) == 1
    assert client.calls == []


# --------------------------------------------------------------------------- #
# OBSERVE: MCP output rides the same UNTRUSTED fence as every tool
# --------------------------------------------------------------------------- #


def test_mcp_output_is_wrapped_untrusted_in_the_conversation(tmp_path):
    script = [_text("", [_echo_call()]), _text("done")]
    engine, _client = _engine(
        tmp_path, script, mode=Mode.MANUAL, broker=_answering_broker("approve")
    )
    events = drive(engine, "use the mcp echo")
    assert isinstance(events[-1], TurnCompleted)

    fed_back = [
        m.content
        for m in engine._conversation
        if "[UNTRUSTED source=mcp__fake__echo" in (m.content or "")
    ]
    assert len(fed_back) == 1  # the tool result reached the model fenced, once
    assert "echo: hi" in fed_back[0]
