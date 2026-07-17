"""MCPTool adapter + MCPManager unit tests — no subprocesses.

The client seam is duck-typed (``MCPTool`` only needs ``call_tool``;
``MCPManager`` needs ``server``/``list_tools``/``aclose``), so a tiny in-test
FakeClient covers namespacing, risk, spec shape, content extraction, and the
manager's fault isolation without spawning anything.
"""

from __future__ import annotations

import asyncio

from ironcore.config.settings import Settings
from ironcore.safety.risk import ToolRisk
from ironcore.tools.base import ToolRegistry
from ironcore.tools.mcp import (
    MAX_OUTPUT_CHARS,
    MCPError,
    MCPManager,
    MCPTool,
    mcp_tool_name,
)

ECHO_SPEC = {
    "name": "echo",
    "description": "Echo text back.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
}


class FakeClient:
    """Duck-typed stand-in for MCPClient."""

    def __init__(self, server="fake", *, result=None, tools=None, exc=None):
        self.server = server
        self.result = result if result is not None else {"content": []}
        self.tools = tools if tools is not None else [ECHO_SPEC]
        self.exc = exc
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def list_tools(self):
        if self.exc is not None:
            raise self.exc
        return self.tools

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.exc is not None:
            raise self.exc
        return self.result

    async def aclose(self):
        self.closed = True


def _tool(client=None, **kwargs) -> MCPTool:
    defaults = dict(server="fake", remote_name="echo", description="Echo.")
    defaults.update(kwargs)
    return MCPTool(client=client or FakeClient(), **defaults)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# naming, risk, spec shape
# --------------------------------------------------------------------------- #


def test_name_is_namespaced_and_sanitized():
    assert mcp_tool_name("My Server", "read/file") == "mcp__My_Server__read_file"
    tool = _tool(server="My Server", remote_name="read/file")
    assert tool.name == "mcp__My_Server__read_file"


def test_empty_name_parts_fall_back():
    assert mcp_tool_name("", "") == "mcp__server__tool"


def test_risk_is_net():
    assert _tool().risk is ToolRisk.NET  # worst-case honest; never auto-allowed


def test_spec_is_openai_function_format():
    schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    spec = _tool(input_schema=schema).spec()
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "mcp__fake__echo"
    assert spec["function"]["parameters"] == schema
    assert spec["function"]["description"].startswith("[MCP:fake]")


def test_missing_input_schema_becomes_empty_object_schema():
    tool = _tool(input_schema=None)
    assert tool.parameters == {"type": "object", "properties": {}}


def test_long_description_is_capped():
    tool = _tool(description="verbose " * 200)
    assert len(tool.description) < 400
    assert tool.description.endswith("...")


# --------------------------------------------------------------------------- #
# run(): content extraction + failure honesty (never raises)
# --------------------------------------------------------------------------- #


def test_run_concatenates_text_and_marks_non_text():
    client = FakeClient(
        result={
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                {"type": "text", "text": "world"},
            ]
        }
    )
    result = _run(_tool(client).run(text="hi"))
    assert result.ok
    assert result.output == "hello\n[image content omitted]\nworld"
    assert client.calls == [("echo", {"text": "hi"})]  # kwargs forwarded verbatim


def test_run_is_error_result_maps_to_ok_false():
    client = FakeClient(result={"content": [{"type": "text", "text": "kaboom"}], "isError": True})
    result = _run(_tool(client).run())
    assert not result.ok
    assert result.error == "kaboom"
    assert "kaboom" in result.output  # the model still sees the failure text


def test_run_caps_output_with_honest_marker():
    huge = "x" * (MAX_OUTPUT_CHARS + 500)
    client = FakeClient(result={"content": [{"type": "text", "text": huge}]})
    result = _run(_tool(client).run())
    assert result.ok
    assert len(result.output) < MAX_OUTPUT_CHARS + 100
    assert "[truncated: 500 more chars]" in result.output


def test_run_mcp_error_becomes_ok_false_never_raises():
    client = FakeClient(exc=MCPError("server went away"))
    result = _run(_tool(client).run(text="x"))
    assert not result.ok
    assert "server went away" in result.error
    assert "server went away" in result.output  # mirrored: the model sees why


def test_run_os_error_becomes_ok_false():
    client = FakeClient(exc=OSError("pipe broke"))
    result = _run(_tool(client).run())
    assert not result.ok and "pipe broke" in result.error


def test_run_tolerates_malformed_result_shapes():
    client = FakeClient(result={"content": "not-a-list"})
    result = _run(_tool(client).run())
    assert result.ok and result.output == ""


# --------------------------------------------------------------------------- #
# MCPManager: from_settings + register_into + aclose
# --------------------------------------------------------------------------- #


def test_from_settings_builds_stdio_clients_and_skips_the_rest():
    settings = Settings.model_validate(
        {
            "mcp": {
                "servers": {
                    "a": {"command": "server-a"},
                    "b": {"command": "server-b", "enabled": False},
                    "c": {"url": "http://example.com/mcp"},
                }
            }
        }
    )
    manager = MCPManager.from_settings(settings)
    assert [c.server for c in manager.clients] == ["a"]  # enabled stdio only
    assert any("'c'" in note and "url-only" in note for note in manager.notes)
    assert not any("'b'" in note for note in manager.notes)  # deliberate off switch: silent


def test_register_into_registers_namespaced_tools_with_notes():
    registry = ToolRegistry()
    manager = MCPManager([FakeClient("fake")])
    notes = _run(manager.register_into(registry))
    tool = registry.get("mcp__fake__echo")
    assert tool is not None and tool.risk is ToolRisk.NET
    assert any("1 tool(s) registered" in n and "mcp__fake__echo" in n for n in notes)


def test_register_into_isolates_a_failing_server():
    registry = ToolRegistry()
    bad = FakeClient("bad", exc=MCPError("spawn failed"))
    good = FakeClient("good")
    notes = _run(MCPManager([bad, good]).register_into(registry))
    assert registry.get("mcp__good__echo") is not None  # the good server still lands
    assert any("'bad' failed" in n and "spawn failed" in n for n in notes)


def test_register_into_skips_duplicate_names():
    registry = ToolRegistry()
    registry.register(_tool())  # mcp__fake__echo already taken
    notes = _run(MCPManager([FakeClient("fake")]).register_into(registry))
    assert any("skipped duplicate" in n and "mcp__fake__echo" in n for n in notes)
    assert any("0 tool(s) registered" in n for n in notes)


def test_register_into_ignores_nameless_specs():
    registry = ToolRegistry()
    client = FakeClient("fake", tools=[{"description": "no name"}, ECHO_SPEC])
    _run(MCPManager([client]).register_into(registry))
    assert len([t for t in registry.all() if t.name.startswith("mcp__")]) == 1


def test_aclose_closes_every_client_despite_failures():
    class ExplodingClient(FakeClient):
        async def aclose(self):
            raise RuntimeError("close failed")

    ok = FakeClient("ok")
    _run(MCPManager([ExplodingClient("boom"), ok]).aclose())
    assert ok.closed  # the second client still closed; nothing raised
