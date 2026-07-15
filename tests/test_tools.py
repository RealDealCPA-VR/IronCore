"""Tool registry contract."""

import asyncio

import pytest

from ironcore.safety.risk import ToolRisk
from ironcore.tools.base import Tool, ToolRegistry, ToolResult


class EchoTool(Tool):
    name = "echo"
    description = "Echo the input string back. Example: echo(text='hi') -> 'hi'"
    risk = ToolRisk.READ
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, **kwargs):
        return ToolResult(ok=True, output=kwargs["text"])


def test_register_get_and_specs():
    registry = ToolRegistry()
    registry.register(EchoTool())
    assert registry.get("echo") is not None
    (spec,) = registry.specs()
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "echo"
    assert spec["function"]["parameters"]["required"] == ["text"]


def test_duplicate_registration_rejected():
    registry = ToolRegistry()
    registry.register(EchoTool())
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(EchoTool())


def test_tool_runs():
    result = asyncio.run(EchoTool().run(text="hello"))
    assert result.ok
    assert result.output == "hello"
