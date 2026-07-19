"""web_search (PKG-5): DuckDuckGo-HTML parsing, caps, redaction, NET gating.

Hermetic like the fetch_url tests — every request is served by an injected
httpx.MockTransport, so nothing touches the real network. Async pattern:
asyncio.run (no pytest-asyncio).
"""

import asyncio

import httpx

from ironcore.safety.modes import Mode
from ironcore.safety.policy import Decision, decide
from ironcore.safety.risk import ToolRisk
from ironcore.tools.search import (
    MAX_RESULTS,
    WebSearchTool,
    _build_search_url,
    _clean_url,
)

SEARCH_URL = "https://search.example/html/"


def run(tool, **kwargs):
    return asyncio.run(tool.run(**kwargs))


def _result_block(title: str, target: str, snippet: str) -> str:
    """One DuckDuckGo-HTML result: a uddg-wrapped title link + a snippet link."""
    uddg = httpx.QueryParams({"uddg": target, "rut": "x"})
    return (
        '<div class="result results_links results_links_deep web-result">'
        '<div class="links_main">'
        f'<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?{uddg}">{title}</a>'
        f'<a class="result__snippet" href="//duckduckgo.com/l/?{uddg}">{snippet}</a>'
        "</div></div>"
    )


def _page(*blocks: str) -> str:
    return "<html><body>" + "".join(blocks) + "</body></html>"


def _serving(html: str, *, status: int = 200, sink: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if sink is not None:
            sink.append(str(request.url))
        return httpx.Response(status, text=html, headers={"content-type": "text/html"})

    return WebSearchTool(SEARCH_URL, transport=httpx.MockTransport(handler))


# --- happy path: parsed results, redirect-decoded urls -------------------------


def test_query_returns_parsed_title_url_snippet():
    html = _page(
        _result_block("First Result", "https://example.com/one", "The first blurb."),
        _result_block("Second Result", "https://example.org/two", "The second blurb."),
    )
    result = run(_serving(html), query="anything")
    assert result.ok
    assert "1. First Result" in result.output
    assert "https://example.com/one" in result.output  # uddg redirect decoded
    assert "The first blurb." in result.output
    assert "2. Second Result" in result.output
    assert "https://example.org/two" in result.output
    # the DuckDuckGo redirector must never surface as a "result" url
    assert "duckduckgo.com/l" not in result.output
    assert result.data["results"] == 2
    assert result.data["status"] == 200


def test_query_is_sent_as_the_q_parameter():
    sink: list[str] = []
    run(_serving(_page(), sink=sink), query="python asyncio timeout")
    assert sink and "q=python" in sink[0] and "asyncio" in sink[0]


def test_endpoint_own_params_are_preserved():
    url = _build_search_url("https://searx.example/search?format=html&engines=ddg", "cats")
    params = httpx.QueryParams(httpx.URL(url).query)
    assert params.get("q") == "cats"
    assert params.get("format") == "html"
    assert params.get("engines") == "ddg"


def test_no_results_is_reported_honestly():
    result = run(_serving(_page("<div>nothing here</div>")), query="obscure")
    assert result.ok
    assert result.output == "(no results)"
    assert result.data["results"] == 0


# --- caps + redaction ----------------------------------------------------------


def test_max_results_caps_the_output():
    blocks = [
        _result_block(f"Result {n}", f"https://example.com/{n}", f"blurb {n}") for n in range(10)
    ]
    result = run(_serving(_page(*blocks)), query="many", max_results=3)
    assert result.ok
    assert result.data["results"] == 3
    assert "3. Result 2" in result.output
    assert "4. Result 3" not in result.output  # capped


def test_max_results_is_clamped_to_the_absolute_cap():
    result = run(_serving(_page()), query="x", max_results=MAX_RESULTS * 5)
    assert result.ok  # clamped internally, no error


def test_output_is_secret_redacted():
    leak = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"  # openai-key-shaped
    html = _page(_result_block("Leaky", "https://example.com/x", f"token {leak} here"))
    result = run(_serving(html), query="secret")
    assert result.ok
    assert leak not in result.output
    assert "[redacted:openai-key]" in result.output


# --- NET policy is inherited untouched -----------------------------------------


def test_web_search_is_net_risk_and_asks_even_in_auto():
    assert WebSearchTool(SEARCH_URL).risk is ToolRisk.NET
    # NET is never auto-allowed: the frozen policy asks in AUTO (SAFETY §3).
    assert decide(Mode.AUTO, ToolRisk.NET) is Decision.ASK
    assert decide(Mode.PLAN, ToolRisk.NET) is Decision.DENY


# --- failure modes never raise -------------------------------------------------


def test_http_error_status_is_ok_false():
    result = run(_serving(_page(), status=503), query="down")
    assert not result.ok
    assert "HTTP 503" in result.error
    assert result.data["status"] == 503


def test_network_error_returns_error_not_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    tool = WebSearchTool(SEARCH_URL, transport=httpx.MockTransport(handler))
    result = run(tool, query="x")
    assert not result.ok
    assert "ConnectError" in result.error


def test_missing_or_blank_query_is_rejected_without_network():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        calls.append(str(request.url))
        return httpx.Response(200, text="x")

    tool = WebSearchTool(SEARCH_URL, transport=httpx.MockTransport(handler))
    assert not run(tool).ok  # no query
    assert not run(tool, query="   ").ok  # blank
    assert not run(tool, query="ok", max_results=0).ok
    assert not run(tool, query="ok", max_results=True).ok  # bool is not a count
    assert calls == []


def test_non_http_search_endpoint_is_rejected():
    tool = WebSearchTool("file:///etc/hosts")
    result = run(tool, query="x")
    assert not result.ok
    assert "http" in result.error


# --- unit: url cleaning --------------------------------------------------------


def test_clean_url_unwraps_and_filters():
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fsite.test%2Fp&rut=z"
    assert _clean_url(wrapped) == "https://site.test/p"
    assert _clean_url("https://plain.test/a") == "https://plain.test/a"
    assert _clean_url("//cdn.test/x") == "https://cdn.test/x"
    assert _clean_url("javascript:alert(1)") is None
    assert _clean_url("/relative/path") is None
    assert _clean_url("") is None
