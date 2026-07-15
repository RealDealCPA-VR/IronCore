"""Network fetch tool: fetch_url (SPEC §6.1, NET risk).

RULES
-----
- The ONLY NET-risk tool in the core suite. It is never registered unless
  ``safety.network_tools`` is true (see ``ironcore.tools.default``) — an
  off NET tool is not merely gated, the model never sees it. The tool
  itself stays self-contained and applies no policy beyond the scheme
  check below.
- Only ``http://`` and ``https://`` URLs are fetched. Any other scheme
  (``file://``, ``ftp://``, ...) returns ``ToolResult(ok=False)`` before
  any I/O happens.
- The body is STREAMED and capped at ``max_bytes`` (clamped to
  ``MAX_FETCH_BYTES``), so a huge response never lands in memory. The
  truncation note is honest: exact remaining bytes when Content-Length
  says so, otherwise the cap that was hit.
- Network, timeout, and protocol errors become ``ToolResult(ok=False,
  error=...)`` — this tool never raises for the outside world's failures.
  Non-2xx statuses mirror the shell tool's nonzero-exit convention:
  ``ok=False`` with the (capped) body still in ``output``.
- ``transport=`` is an injection seam (same convention as
  ``ironcore/providers``): tests pass ``httpx.MockTransport`` and touch
  zero real network.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from ironcore.safety.risk import ToolRisk
from ironcore.tools.base import Tool, ToolResult

#: Default cap on the returned body when the model gives no max_bytes.
DEFAULT_MAX_BYTES = 50_000
#: Absolute cap; a larger max_bytes argument is clamped to this.
MAX_FETCH_BYTES = 500_000
#: Whole-request timeout (connect + read), seconds. Not model-controllable.
DEFAULT_TIMEOUT_S = 30.0

_ALLOWED_SCHEMES = ("http", "https")


class FetchUrlTool(Tool):
    """GET an http(s) URL and return the response body as capped text."""

    name = "fetch_url"
    description = (
        "Fetch a URL over the network with an HTTP GET and return the response body as text. "
        "Only http:// and https:// URLs are allowed. Optional max_bytes caps how much of the "
        f"body is returned (default {DEFAULT_MAX_BYTES}). "
        "Example: fetch_url(url='https://example.com/data.json')."
    )
    risk = ToolRisk.NET
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Full URL to fetch, e.g. 'https://example.com/page'. "
                "Must start with http:// or https://.",
            },
            "max_bytes": {
                "type": "integer",
                "description": f"Maximum body bytes to return (default {DEFAULT_MAX_BYTES}, "
                f"max {MAX_FETCH_BYTES}).",
            },
        },
        "required": ["url"],
    }

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Injection seam: tests pass httpx.MockTransport; None = real network.
        self._transport = transport

    async def run(self, **kwargs: Any) -> ToolResult:
        url = kwargs.get("url")
        if not isinstance(url, str) or not url:
            return ToolResult(ok=False, output="", error="'url' (string) is required")
        max_bytes = kwargs.get("max_bytes", DEFAULT_MAX_BYTES)
        if not isinstance(max_bytes, int) or max_bytes < 1:
            return ToolResult(ok=False, output="", error="'max_bytes' must be an integer >= 1")
        max_bytes = min(max_bytes, MAX_FETCH_BYTES)

        parts = urlsplit(url)
        if parts.scheme not in _ALLOWED_SCHEMES:
            return ToolResult(
                ok=False,
                output="",
                error=f"unsupported URL scheme {parts.scheme or '(none)'!r}: "
                "only http:// and https:// are allowed",
            )
        if not parts.netloc:
            return ToolResult(ok=False, output="", error=f"URL has no host: {url!r}")

        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=True,
                timeout=httpx.Timeout(DEFAULT_TIMEOUT_S),
            ) as client:
                async with client.stream("GET", url) as response:
                    buf = bytearray()
                    async for chunk in response.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            break  # cap hit — stop downloading, keep what we have
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as exc:
            # TimeoutException/ConnectError/etc. all descend from HTTPError.
            return ToolResult(
                ok=False, output="", error=f"fetch failed: {type(exc).__name__}: {exc}"
            )

        truncated = len(buf) > max_bytes
        text = bytes(buf[:max_bytes]).decode("utf-8", errors="replace")
        if truncated:
            text += f"\n{_truncation_note(response, max_bytes)}"
        elif not text:
            text = "(empty response body)"

        status = response.status_code
        data = {
            "status": status,
            "url": str(response.url),
            "bytes": min(len(buf), max_bytes),
            "truncated": truncated,
            "content_type": response.headers.get("content-type", ""),
        }
        if response.is_success:
            return ToolResult(ok=True, output=text, data=data)
        # Non-2xx mirrors shell's nonzero exit: honest failure, body preserved.
        reason = response.reason_phrase
        error = f"HTTP {status}" + (f" {reason}" if reason else "")
        return ToolResult(ok=False, output=text, error=error, data=data)


def _truncation_note(response: httpx.Response, max_bytes: int) -> str:
    """Honest cap marker: exact remaining count when Content-Length says so."""
    try:
        total = int(response.headers["content-length"])
    except (KeyError, ValueError):
        total = -1
    if total > max_bytes:
        return f"... [truncated: {total - max_bytes} more bytes]"
    return f"... [truncated at {max_bytes} bytes]"
