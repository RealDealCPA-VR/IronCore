"""MCPClient against a REAL subprocess (tests/tools/fake_mcp_server.py).

Every case spawns the stdlib-only fake server via ``sys.executable`` — fully
hermetic and Windows-safe, zero network — and drives the actual
spawn -> initialize -> tools/list -> tools/call -> close lifecycle. Async is
driven with ``asyncio.run`` (repo convention, no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from ironcore.tools.mcp import MCPClient, MCPError

SERVER = str(Path(__file__).resolve().parent / "fake_mcp_server.py")


def _client(*, timeout_s: float = 15.0, env: dict[str, str] | None = None) -> MCPClient:
    return MCPClient(
        "fake", command=sys.executable, args=[SERVER], env=env, timeout_s=timeout_s
    )


def test_client_is_lazy_no_subprocess_until_first_call():
    client = _client()
    assert client._proc is None  # nothing spawned at construction


def test_initialize_and_list_tools():
    async def run():
        client = _client()
        try:
            return await client.list_tools()
        finally:
            await client.aclose()

    tools = asyncio.run(run())
    assert [t["name"] for t in tools] == ["echo", "boom", "slow"]
    assert tools[0]["inputSchema"]["type"] == "object"


def test_list_tools_follows_next_cursor_pagination():
    async def run():
        client = _client(env={"FAKE_MCP_PAGINATE": "1"})
        try:
            return await client.list_tools()
        finally:
            await client.aclose()

    tools = asyncio.run(run())
    assert [t["name"] for t in tools] == ["echo", "boom", "slow"]  # 3 pages, stitched


def test_call_tool_roundtrip():
    async def run():
        client = _client()
        try:
            return await client.call_tool("echo", {"text": "hello-mcp"})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result["content"][0]["text"] == "echo: hello-mcp"
    assert not result.get("isError")


def test_is_error_result_returns_normally_not_raises():
    async def run():
        client = _client()
        try:
            return await client.call_tool("boom", {})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result.get("isError") is True  # a TOOL error is data, not an exception
    assert result["content"][0]["text"] == "kaboom"


def test_json_rpc_error_reply_raises_mcp_error():
    async def run():
        client = _client()
        try:
            await client.call_tool("no_such_tool", {})
        finally:
            await client.aclose()

    with pytest.raises(MCPError, match="no_such_tool"):
        asyncio.run(run())


def test_timeout_raises_mcp_error_with_timed_out():
    async def run():
        client = _client(timeout_s=0.5, env={"FAKE_MCP_SLEEP": "3"})
        try:
            await client.call_tool("echo", {"text": "too slow"})
        finally:
            await client.aclose()

    with pytest.raises(MCPError, match="timed out"):
        asyncio.run(run())


def test_garbage_stdout_lines_are_skipped():
    async def run():
        client = _client(env={"FAKE_MCP_GARBAGE": "1"})
        try:
            return await client.call_tool("echo", {"text": "still works"})
        finally:
            await client.aclose()

    result = asyncio.run(run())
    assert result["content"][0]["text"] == "echo: still works"


def test_dead_server_raises_mcp_error():
    async def run():
        client = MCPClient(
            "dead",
            command=sys.executable,
            args=["-c", "import sys; sys.exit(0)"],  # exits before the handshake
            timeout_s=15.0,
        )
        try:
            await client.list_tools()
        finally:
            await client.aclose()

    with pytest.raises(MCPError):
        asyncio.run(run())


def test_unspawnable_command_raises_mcp_error():
    async def run():
        client = MCPClient("ghost", command="definitely-not-a-real-command-xyz")
        try:
            await client.list_tools()
        finally:
            await client.aclose()

    with pytest.raises(MCPError, match="failed to start"):
        asyncio.run(run())


def test_aclose_terminates_the_child():
    async def run():
        client = _client()
        await client.list_tools()
        proc = client._proc
        assert proc is not None and proc.returncode is None  # alive mid-session
        await client.aclose()
        return proc.returncode

    assert asyncio.run(run()) is not None  # reaped: exit code recorded


def test_calls_after_close_raise_instead_of_respawning():
    async def run():
        client = _client()
        await client.list_tools()
        await client.aclose()
        await client.call_tool("echo", {"text": "zombie?"})

    with pytest.raises(MCPError, match="closed"):
        asyncio.run(run())
