"""MCP tool servers: a stdio JSON-RPC client + adapter into the gated registry.

RULES
-----
- Transport is newline-delimited JSON-RPC 2.0 over a child process's
  stdin/stdout (the MCP "stdio" transport), hand-rolled on stdlib asyncio —
  no MCP SDK: IronCore needs exactly three methods (``initialize``,
  ``tools/list``, ``tools/call``) and the dependency footprint is
  load-bearing (pure-python wheel; textual/httpx/pydantic/pyyaml only).
- The child is spawned with ``create_subprocess_exec`` — NEVER a shell, so
  there is no injection surface. ``shutil.which`` resolves launcher shims
  (``command = "npx.cmd"`` works on Windows). stderr goes to DEVNULL
  (servers log there; an unread pipe would deadlock); non-JSON stdout lines
  are skipped — some servers print human noise around the protocol.
- Every remote tool surfaces as ``mcp__<server>__<tool>`` with
  ``ToolRisk.NET`` — worst-case honest (CONTRACTS §1): an MCP server is an
  arbitrary subprocess that typically proxies remote APIs, and NET is the
  strictest class (never auto-allowed, denied in PLAN). Like ``fetch_url``,
  MCP tools are only *registered* when ``safety.network_tools`` is true —
  the wiring (tui/app.py) enforces that; nothing here gates or prints
  (CONTRACTS §3). The engine's OBSERVE step already wraps every tool output
  as UNTRUSTED and runs injection detection — MCP output gets both for free.
- ``MCPTool.run`` never raises for the outside world's failures: timeouts,
  protocol errors, and dead servers all become ``ToolResult(ok=False)``,
  with the message mirrored into ``output`` so the MODEL sees why (the
  engine feeds only ``output`` back; ``error`` is UI-facing).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from typing import Any

from ironcore import __version__
from ironcore.config.settings import Settings
from ironcore.safety.risk import ToolRisk
from ironcore.tools.base import Tool, ToolRegistry, ToolResult

#: MCP protocol revision pinned in the initialize handshake. We accept the
#: server's reply without strict negotiation — fine for tools/list + call.
PROTOCOL_VERSION = "2024-11-05"
#: Hard cap on one stdout line from the server: a hostile/huge reply raises
#: (becomes MCPError) instead of eating memory.
STREAM_LIMIT = 5_000_000
#: Cap on model-visible output per call (fetch_url's DEFAULT_MAX_BYTES scale).
MAX_OUTPUT_CHARS = 50_000
#: tools/list pagination bound — a server that never ends its cursor chain
#: cannot spin us forever.
MAX_LIST_PAGES = 16
#: Description cap per adapted tool: the catalog rides every prompt, and many
#: servers ship paragraph-long descriptions that would squeeze small contexts.
MAX_DESCRIPTION_CHARS = 300
#: Seconds a well-behaved server gets to exit on stdin close, then per
#: escalation rung (terminate -> kill) during aclose().
_CLOSE_GRACE_S = 3.0

_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


class MCPError(Exception):
    """An MCP server misbehaved: spawn failure, transport death, malformed or
    error reply, or timeout. ``MCPTool`` turns this into ``ToolResult(ok=False)``;
    it never escapes to the engine."""


def mcp_tool_name(server: str, remote_name: str) -> str:
    """``mcp__<server>__<tool>`` with both parts sanitized to ``[A-Za-z0-9_-]``
    so the name is safe in OpenAI function specs, guided-decoding enums, and
    the IRONCALL catalog alike."""
    s = _NAME_UNSAFE.sub("_", server) or "server"
    t = _NAME_UNSAFE.sub("_", remote_name) or "tool"
    return f"mcp__{s}__{t}"


class MCPClient:
    """Lazy stdio client for ONE MCP server.

    Nothing is spawned at construction: the subprocess starts (and the
    ``initialize`` handshake runs) on the first ``list_tools``/``call_tool``.
    A server that later dies stays dead — subsequent calls raise ``MCPError``
    rather than respawn-looping. One request/response is in flight at a time
    (an asyncio lock), so replies cannot interleave; stale replies from a
    timed-out call are skipped by id on the next read.
    """

    def __init__(
        self,
        server: str,
        *,
        command: str,
        args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.server = server
        self._command = command
        self._args = list(args)
        self._env = dict(env) if env else None
        self._timeout_s = timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._started = False
        self._next_id = 0
        self._lock = asyncio.Lock()

    # -- public API -----------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """All tool specs the server advertises (follows ``nextCursor``,
        bounded to ``MAX_LIST_PAGES`` pages). Raises ``MCPError``."""
        async with self._lock:
            await self._ensure_started()
            tools: list[dict[str, Any]] = []
            cursor: str | None = None
            for _ in range(MAX_LIST_PAGES):
                params = {"cursor": cursor} if cursor else {}
                result = await self._request("tools/list", params)
                page = result.get("tools")
                if isinstance(page, list):
                    tools.extend(item for item in page if isinstance(item, dict))
                cursor = result.get("nextCursor") or None
                if cursor is None:
                    break
            return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One ``tools/call``. Returns the raw MCP result dict (``content``,
        ``isError``); a JSON-RPC *error* reply raises ``MCPError``."""
        async with self._lock:
            await self._ensure_started()
            return await self._request("tools/call", {"name": name, "arguments": arguments})

    async def aclose(self) -> None:
        """Shut the server down: stdin close (a well-behaved server exits on
        EOF) -> ``terminate()`` -> ``kill()``. Never raises."""
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
            return
        except TimeoutError:
            pass
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=_CLOSE_GRACE_S)
            return
        except TimeoutError:
            pass
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=_CLOSE_GRACE_S)
        except TimeoutError:  # pragma: no cover — an unkillable child
            pass

    # -- lifecycle ------------------------------------------------------------

    async def _ensure_started(self) -> None:
        """Spawn + handshake on first use; raise for a dead server after."""
        if self._proc is not None:
            if self._proc.returncode is None:
                return
            raise MCPError(
                f"mcp server {self.server!r} exited with code {self._proc.returncode}"
            )
        if self._started:  # closed (or handshake-failed) — do not respawn
            raise MCPError(f"mcp server {self.server!r} is closed")
        self._started = True
        # which() resolves PATH lookups AND Windows launcher shims (npx.cmd);
        # an absolute/relative command falls through unchanged.
        resolved = shutil.which(self._command) or self._command
        env = {**os.environ, **self._env} if self._env else None
        try:
            self._proc = await asyncio.create_subprocess_exec(
                resolved,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=STREAM_LIMIT,
                env=env,
            )
        except OSError as exc:
            raise MCPError(f"failed to start mcp server {self.server!r}: {exc}") from exc
        try:
            await self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "ironcore", "version": __version__},
                },
            )
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await self._drain()
        except MCPError:
            await self.aclose()
            raise

    # -- JSON-RPC over NDJSON -------------------------------------------------

    def _write(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:  # pragma: no cover — guarded by callers
            raise MCPError(f"mcp server {self.server!r} is not running")
        proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")

    async def _drain(self) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:  # pragma: no cover — guarded by callers
            raise MCPError(f"mcp server {self.server!r} is not running")
        try:
            await proc.stdin.drain()
        except (ConnectionError, OSError) as exc:
            raise MCPError(f"mcp server {self.server!r} transport failed: {exc}") from exc

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Write one request line, read lines until the matching id."""
        self._next_id += 1
        req_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)
        await self._drain()
        try:
            return await asyncio.wait_for(
                self._read_response(req_id, method), timeout=self._timeout_s
            )
        except TimeoutError as exc:
            raise MCPError(
                f"mcp server {self.server!r} timed out after {self._timeout_s:g}s on {method!r}"
            ) from exc

    async def _read_response(self, req_id: int, method: str) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdout is None:  # pragma: no cover — guarded by callers
            raise MCPError(f"mcp server {self.server!r} is not running")
        while True:
            try:
                line = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                raise MCPError(
                    f"mcp server {self.server!r} sent a line over {STREAM_LIMIT} bytes"
                ) from exc
            if not line:
                raise MCPError(
                    f"mcp server {self.server!r} closed its stdout during {method!r}"
                )
            stripped = line.strip()
            if not stripped:
                continue
            try:
                msg = json.loads(stripped)
            except json.JSONDecodeError:
                continue  # human log noise on stdout — skip, stay on protocol
            if not isinstance(msg, dict) or "method" in msg or msg.get("id") != req_id:
                # A "method" key makes it a server-initiated REQUEST or a
                # notification, never a response -- and a server numbering its
                # own requests from 1 collides with our ids. Without this,
                # such a message passes the id check, has no "result", and
                # returns {}: tools/list would register zero tools and say so
                # cheerfully. We advertise no capabilities, so a well-behaved
                # server never does this -- which is exactly the assumption
                # this module refuses to make anywhere else.
                continue  # or a stale reply from a timed-out call
            if "error" in msg:
                err = msg.get("error")
                detail = err.get("message", err) if isinstance(err, dict) else err
                raise MCPError(f"mcp server {self.server!r} error on {method!r}: {detail}")
            result = msg.get("result")
            return result if isinstance(result, dict) else {}


def _content_text(content: Any) -> str:
    """Flatten an MCP ``content`` list to model-visible text. Non-text items
    become an honest omission marker rather than silently vanishing."""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        else:
            parts.append(f"[{item.get('type') or 'unknown'} content omitted]")
    return "\n".join(parts)


class MCPTool(Tool):
    """One remote MCP tool adapted to the frozen Tool contract (CONTRACTS §3).

    ``client`` is duck-typed (needs ``call_tool``) so tests inject a fake;
    production passes an ``MCPClient``. One risk class, worst-case honest:
    NET — see the module docstring for why.
    """

    risk = ToolRisk.NET

    def __init__(
        self,
        *,
        client: Any,
        server: str,
        remote_name: str,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._remote_name = remote_name
        self.name = mcp_tool_name(server, remote_name)
        desc = " ".join((description or f"MCP tool {remote_name!r}.").split())
        if len(desc) > MAX_DESCRIPTION_CHARS:
            desc = desc[: MAX_DESCRIPTION_CHARS - 3] + "..."
        self.description = f"[MCP:{server}] {desc}"
        if isinstance(input_schema, dict) and input_schema:
            self.parameters = input_schema
        else:
            self.parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        try:
            result = await self._client.call_tool(self._remote_name, kwargs)
        except (MCPError, OSError, TimeoutError) as exc:
            # Mirror the message into output: the engine feeds only output
            # back to the model; error is event/UI-facing.
            return ToolResult(ok=False, output=f"[mcp error] {exc}", error=str(exc))
        if not isinstance(result, dict):
            result = {}
        output = _content_text(result.get("content"))
        if len(output) > MAX_OUTPUT_CHARS:
            dropped = len(output) - MAX_OUTPUT_CHARS
            output = output[:MAX_OUTPUT_CHARS] + f"\n... [truncated: {dropped} more chars]"
        if result.get("isError"):
            first = output.strip().splitlines()
            error = first[0] if first else "the MCP server reported a tool error"
            return ToolResult(ok=False, output=output, error=error)
        return ToolResult(ok=True, output=output)


class MCPManager:
    """Owns a session's MCP clients: built from settings, registered into the
    live ToolRegistry, closed at shutdown. Registration is per-server
    fault-isolated and NEVER raises — one bad server must not sink the rest
    (or the app boot)."""

    def __init__(self, clients: Sequence[Any], *, notes: Sequence[str] = ()) -> None:
        self.clients = list(clients)
        #: config-time notes (skipped entries), prepended to register_into()'s.
        self.notes = list(notes)

    @classmethod
    def from_settings(cls, settings: Settings) -> MCPManager:
        """Clients for every enabled stdio server in ``[mcp.servers.*]``.
        url-only entries are skipped with a note (http transport is v-next);
        ``enabled = false`` is a deliberate off switch and skips silently."""
        clients: list[MCPClient] = []
        notes: list[str] = []
        for name, server in settings.mcp.servers.items():
            if not server.enabled:
                continue
            if not server.command:
                notes.append(
                    f"[mcp] server {name!r} skipped: url-only entries are not supported "
                    "yet (stdio only -- set 'command')"
                )
                continue
            clients.append(
                MCPClient(
                    name,
                    command=server.command,
                    args=server.args,
                    env=server.env,
                    timeout_s=server.timeout_s,
                )
            )
        return cls(clients, notes=notes)

    async def register_into(self, registry: ToolRegistry) -> list[str]:
        """Connect every server (lazily starting it), wrap its tools, register
        them. Returns human-readable note lines; never raises."""
        notes = list(self.notes)
        for client in self.clients:
            server = getattr(client, "server", "?")
            try:
                specs = await client.list_tools()
            except Exception as exc:  # noqa: BLE001 — fault isolation per server
                notes.append(f"[mcp] server {server!r} failed: {exc}")
                continue
            registered: list[str] = []
            skipped: list[str] = []
            for spec in specs:
                remote = str(spec.get("name") or "")
                if not remote:
                    continue
                tool = MCPTool(
                    client=client,
                    server=server,
                    remote_name=remote,
                    description=str(spec.get("description") or ""),
                    input_schema=spec.get("inputSchema"),
                )
                try:
                    registry.register(tool)
                except ValueError:
                    skipped.append(tool.name)  # duplicate name — first registration wins
                    continue
                registered.append(tool.name)
            note = f"[mcp] server {server!r}: {len(registered)} tool(s) registered"
            if registered:
                note += f" ({', '.join(registered)})"
            if skipped:
                note += f"; skipped duplicate name(s): {', '.join(skipped)}"
            notes.append(note)
        return notes

    async def aclose(self) -> None:
        """Close every client. Never raises — this runs during shutdown."""
        for client in self.clients:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                pass
