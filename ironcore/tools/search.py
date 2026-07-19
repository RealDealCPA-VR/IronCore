"""Web search tool: web_search (SPEC §6.1, NET risk).

RULES (mirroring ``tools/fetch.py`` — the two NET tools share a shape)
---------------------------------------------------------------------
- NET-risk, so like ``fetch_url`` it is **never registered** unless
  ``safety.network_tools`` is true (see ``ironcore.tools.default``): an off
  NET tool is not merely gated, the model never sees it. And NET is never
  auto-allowed in any mode — every search ASKS (``safety.policy``), even in
  AUTO. This tool declares an honest ``ToolRisk.NET`` and adds no policy of
  its own beyond the scheme check below.
- The query is sent to a configurable HTML search endpoint (``[tools]
  search_url``) as a ``?q=`` parameter — a SearXNG instance or the DuckDuckGo
  HTML endpoint (the default). Only ``http(s)`` endpoints are contacted.
- The response body is STREAMED and capped at ``MAX_SEARCH_BYTES`` so a huge
  page never lands in memory, then parsed with the **stdlib HTML parser**
  (``html.parser`` — linear, no regex backtracking on attacker-controlled
  markup) into ``(title, url, snippet)`` results. Output is capped to
  ``max_results`` and ``MAX_OUTPUT_CHARS`` and **secret-redacted** before it
  leaves the tool — a scraped page that echoes an API key must not carry it
  back to the model or the transcript.
- Network, timeout, and protocol errors become ``ToolResult(ok=False,
  error=...)``; a non-2xx status is ``ok=False`` with the honest reason. The
  tool never raises for the outside world's failures.
- ``transport=`` is the same injection seam ``ironcore/providers`` and
  ``tools/fetch.py`` use: tests pass ``httpx.MockTransport`` and touch zero
  real network.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ironcore.safety.redact import redact_transcript
from ironcore.safety.risk import ToolRisk
from ironcore.tools.base import Tool, ToolResult

#: Default / maximum number of results returned; a larger request is clamped.
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS = 20
#: Cap on a single result's snippet, and on the whole assembled output.
MAX_SNIPPET_CHARS = 300
MAX_OUTPUT_CHARS = 6_000
#: Cap on the search-response body streamed before parsing (defense against a
#: hostile endpoint; a real results page is far smaller).
MAX_SEARCH_BYTES = 300_000
#: Whole-request timeout (connect + read), seconds. Not model-controllable.
DEFAULT_TIMEOUT_S = 30.0
#: Some HTML search endpoints (DuckDuckGo) serve a challenge to an empty
#: User-Agent; send a plain one so the default endpoint works out of the box.
_USER_AGENT = "Mozilla/5.0 (compatible; IronCore/0.2; +https://github.com/RealDealCPA-VR/IronCore)"

_ALLOWED_SCHEMES = ("http", "https")


class _ResultParser(HTMLParser):
    """Extract ``(title, url, snippet)`` triples from a results page.

    Keyed on the DuckDuckGo-HTML result classes (``result__a`` for the title
    link, ``result__snippet`` for the blurb) — the shape the default endpoint
    and a plain SearXNG HTML theme both emit. Linear and allocation-bounded:
    HTMLParser feeds one event per tag/text run, so there is no backtracking
    on adversarial markup.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._title_idx: int | None = None
        self._snippet_idx: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr = {k: (v or "") for k, v in attrs}
        classes = attr.get("class", "").split()
        if "result__a" in classes:
            url = _clean_url(attr.get("href", ""))
            if url is not None:
                self.results.append({"title": "", "url": url, "snippet": ""})
                self._title_idx = len(self.results) - 1
        elif "result__snippet" in classes and self.results:
            # the snippet belongs to the most recently opened result
            self._snippet_idx = len(self.results) - 1

    def handle_endtag(self, tag: str) -> None:
        if tag != "a":
            return
        if self._title_idx is not None:
            self.results[self._title_idx]["title"] = self.results[self._title_idx]["title"].strip()
            self._title_idx = None
        if self._snippet_idx is not None:
            idx = self._snippet_idx
            self.results[idx]["snippet"] = self.results[idx]["snippet"].strip()
            self._snippet_idx = None

    def handle_data(self, data: str) -> None:
        if self._title_idx is not None:
            self.results[self._title_idx]["title"] += data
        elif self._snippet_idx is not None:
            self.results[self._snippet_idx]["snippet"] += data


def _clean_url(href: str) -> str | None:
    """Resolve a result anchor's href to a plain ``http(s)`` URL, or ``None``.

    DuckDuckGo wraps every result in a redirector (``//duckduckgo.com/l/?uddg=
    <percent-encoded-target>``); unwrap it to the real destination. A
    protocol-relative ``//host/...`` is treated as https. Anything that is not
    an ``http(s)`` URL with a host is dropped, so a ``javascript:`` or relative
    in-site link never reaches the model as a "result".
    """
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    qs = parse_qs(parts.query)
    if qs.get("uddg"):
        target = qs["uddg"][0]  # parse_qs has already percent-decoded it
        tp = urlsplit(target)
        return target if tp.scheme in _ALLOWED_SCHEMES and tp.netloc else None
    if parts.scheme in _ALLOWED_SCHEMES and parts.netloc:
        return href
    return None


def _build_search_url(search_url: str, query: str) -> str:
    """``search_url`` with ``q=<query>`` set (replacing any existing ``q``),
    preserving the endpoint's own params (e.g. a SearXNG ``format=`` or
    ``engines=``)."""
    parts = urlsplit(search_url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "q"]
    pairs.append(("q", query))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))


def _format_results(results: list[dict[str, str]], max_results: int) -> str:
    """Render results as ``N. title / url / snippet`` blocks, capped."""
    if not results:
        return "(no results)"
    blocks: list[str] = []
    for i, result in enumerate(results[:max_results], start=1):
        title = result["title"] or "(untitled)"
        lines = [f"{i}. {title}", f"   {result['url']}"]
        snippet = result["snippet"]
        if snippet:
            if len(snippet) > MAX_SNIPPET_CHARS:
                snippet = snippet[:MAX_SNIPPET_CHARS] + " ..."
            lines.append(f"   {snippet}")
        blocks.append("\n".join(lines))
    text = "\n\n".join(blocks)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n... [truncated]"
    return text


class WebSearchTool(Tool):
    """Search the web via a configured HTML endpoint and return top results."""

    name = "web_search"
    description = (
        "Search the web and return the top results as text (title, url, and a short snippet "
        "each). Use it to find current information or documentation you cannot read from the "
        "workspace. Optional max_results caps how many results are returned "
        f"(default {DEFAULT_MAX_RESULTS}, max {MAX_RESULTS}). "
        "Example: web_search(query='python asyncio timeout best practices')."
    )
    risk = ToolRisk.NET
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for, e.g. 'ruff noqa syntax'.",
            },
            "max_results": {
                "type": "integer",
                "description": f"How many results to return (default {DEFAULT_MAX_RESULTS}, "
                f"max {MAX_RESULTS}).",
            },
        },
        "required": ["query"],
    }

    def __init__(self, search_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # search_url comes from [tools] search_url; transport is the test seam
        # (httpx.MockTransport) — None means the real network.
        self._search_url = search_url
        self._transport = transport

    async def run(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(ok=False, output="", error="'query' (non-empty string) is required")
        max_results = kwargs.get("max_results", DEFAULT_MAX_RESULTS)
        if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results < 1:
            return ToolResult(ok=False, output="", error="'max_results' must be an integer >= 1")
        max_results = min(max_results, MAX_RESULTS)

        if urlsplit(self._search_url).scheme not in _ALLOWED_SCHEMES:
            return ToolResult(
                ok=False,
                output="",
                error=f"search endpoint is not an http(s) URL: {self._search_url!r} "
                "(set [tools] search_url)",
            )
        request_url = _build_search_url(self._search_url, query.strip())

        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=True,
                timeout=httpx.Timeout(DEFAULT_TIMEOUT_S),
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                async with client.stream("GET", request_url) as response:
                    buf = bytearray()
                    async for chunk in response.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_SEARCH_BYTES:
                            break  # cap hit — stop downloading, parse what we have
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as exc:
            return ToolResult(
                ok=False, output="", error=f"search failed: {type(exc).__name__}: {exc}"
            )

        status = response.status_code
        data = {"status": status, "url": str(response.url), "query": query.strip()}
        if not response.is_success:
            reason = response.reason_phrase
            return ToolResult(
                ok=False,
                output="",
                error=f"search endpoint returned HTTP {status}"
                + (f" {reason}" if reason else ""),
                data=data,
            )

        parser = _ResultParser()
        parser.feed(bytes(buf[:MAX_SEARCH_BYTES]).decode("utf-8", errors="replace"))
        parser.close()
        text = _format_results(parser.results, max_results)
        # Defense-in-depth over the engine's outbound redact_context choke point:
        # a scraped page can echo secrets, and this output is both fed to the
        # model and rendered in the transcript.
        text = redact_transcript(text)
        data["results"] = min(len(parser.results), max_results)
        return ToolResult(ok=True, output=text, data=data)
