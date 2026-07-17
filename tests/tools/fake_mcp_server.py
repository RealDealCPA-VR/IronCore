"""A minimal stdio MCP server for the client tests (NOT a pytest module).

Speaks newline-delimited JSON-RPC 2.0 on stdin/stdout using only the stdlib,
so tests spawn it hermetically via ``sys.executable`` on any OS. Supports
``initialize``, ``tools/list`` (optionally paginated), and ``tools/call`` for
three tools: ``echo`` (happy path), ``boom`` (``isError`` result), ``slow``
(plain result — pair with FAKE_MCP_SLEEP for timeouts).

Environment knobs (all optional):
  FAKE_MCP_SLEEP     seconds to sleep before answering any tools/call
  FAKE_MCP_GARBAGE   when set, print a non-JSON log line to stdout at boot
  FAKE_MCP_PAGINATE  when set, tools/list returns one tool per page via nextCursor
"""

from __future__ import annotations

import json
import os
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "boom",
        "description": "Always fails with an isError result.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "slow",
        "description": "Answers eventually.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _reply(msg_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    print(json.dumps(payload), flush=True)


def _list_result(params):
    if not os.environ.get("FAKE_MCP_PAGINATE"):
        return {"tools": TOOLS}
    index = int(params.get("cursor") or 0)
    result = {"tools": [TOOLS[index]]}
    if index + 1 < len(TOOLS):
        result["nextCursor"] = str(index + 1)
    return result


def _call_result(params):
    name = params.get("name")
    args = params.get("arguments") or {}
    sleep_s = float(os.environ.get("FAKE_MCP_SLEEP", "0"))
    if sleep_s:
        time.sleep(sleep_s)
    if name == "echo":
        return {"content": [{"type": "text", "text": f"echo: {args.get('text', '')}"}]}
    if name == "boom":
        return {"content": [{"type": "text", "text": "kaboom"}], "isError": True}
    if name == "slow":
        return {"content": [{"type": "text", "text": "finally"}]}
    return None  # unknown tool -> JSON-RPC error (see main)


def main() -> None:
    if os.environ.get("FAKE_MCP_GARBAGE"):
        print("fake-mcp-server booting... (human log noise on stdout)", flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if msg_id is None:
            continue  # a notification (e.g. notifications/initialized) — no reply
        if method == "initialize":
            _reply(
                msg_id,
                result={
                    "protocolVersion": msg.get("params", {}).get("protocolVersion", ""),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-mcp", "version": "0.0.1"},
                },
            )
        elif method == "tools/list":
            _reply(msg_id, result=_list_result(msg.get("params") or {}))
        elif method == "tools/call":
            result = _call_result(msg.get("params") or {})
            if result is None:
                name = (msg.get("params") or {}).get("name")
                _reply(msg_id, error={"code": -32602, "message": f"unknown tool {name!r}"})
            else:
                _reply(msg_id, result=result)
        else:
            _reply(msg_id, error={"code": -32601, "message": f"method {method!r} not found"})


if __name__ == "__main__":
    main()
