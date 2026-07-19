"""Default toolset assembly (IC-304): settings matrix, valid specs, fetch_url seam.

fetch_url is exercised ONLY through an injected httpx.MockTransport —
zero real network. Async pattern: asyncio.run (no pytest-asyncio).
"""

import asyncio

import httpx

from ironcore.config.settings import Settings
from ironcore.safety.risk import ToolRisk
from ironcore.tools.default import build_default_registry
from ironcore.tools.fetch import DEFAULT_MAX_BYTES, MAX_FETCH_BYTES, FetchUrlTool

LOCAL_TOOLS = {
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "write_file",
    "edit_file",
    "shell",
    "read_image",  # MS-6: always registered — degrade is an honest tool error
    "use_skill",  # PKG-4: always registered when [skills] enabled (the default)
}


def run(tool, **kwargs):
    return asyncio.run(tool.run(**kwargs))


def network_on() -> Settings:
    return Settings.model_validate({"safety": {"network_tools": True}})


# --- registry contents per settings matrix ------------------------------------


def test_default_settings_register_exactly_the_local_tools(tmp_path):
    registry = build_default_registry(Settings(), tmp_path)
    names = {t.name for t in registry.all()}
    assert names == LOCAL_TOOLS
    assert registry.get("fetch_url") is None


def test_use_skill_absent_when_skills_disabled(tmp_path):
    """The [skills] kill switch also drops the use_skill tool from the roster."""
    settings = Settings.model_validate({"skills": {"enabled": False}})
    registry = build_default_registry(settings, tmp_path)
    assert registry.get("use_skill") is None
    assert {t.name for t in registry.all()} == LOCAL_TOOLS - {"use_skill"}


def test_network_tools_true_adds_fetch_url(tmp_path):
    registry = build_default_registry(network_on(), tmp_path)
    names = {t.name for t in registry.all()}
    assert names == LOCAL_TOOLS | {"fetch_url"}
    fetch = registry.get("fetch_url")
    assert isinstance(fetch, FetchUrlTool)
    assert fetch.risk is ToolRisk.NET


def test_assembled_registry_has_unique_names(tmp_path):
    registry = build_default_registry(network_on(), tmp_path)
    names = [t.name for t in registry.all()]
    assert len(names) == len(set(names)) == len(LOCAL_TOOLS) + 1  # + fetch_url


# --- every spec is a valid model-facing function spec --------------------------


def test_every_spec_is_a_valid_json_schema_object(tmp_path):
    registry = build_default_registry(network_on(), tmp_path)
    assert registry.all(), "registry must not be empty"
    for tool in registry.all():
        spec = tool.spec()
        assert spec["type"] == "function"
        fn = spec["function"]
        assert fn["name"] == tool.name and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"].strip()
        assert "Example:" in fn["description"]  # model-facing: example-bearing (SPEC §6.2)
        params = fn["parameters"]
        assert isinstance(params, dict)
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict) and params["properties"]
        for prop in params["properties"].values():
            assert isinstance(prop, dict) and prop.get("type")
        required = params.get("required", [])
        assert isinstance(required, list)
        assert set(required) <= set(params["properties"])


# --- fetch_url: MockTransport-backed behavior ----------------------------------


def test_fetch_returns_body_text_and_status_data():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="hello world", headers={"content-type": "text/plain"})

    result = run(FetchUrlTool(httpx.MockTransport(handler)), url="https://example.com/x")
    assert result.ok
    assert result.output == "hello world"
    assert result.data["status"] == 200
    assert result.data["truncated"] is False
    assert result.data["content_type"] == "text/plain"


def test_fetch_caps_body_with_honest_truncation_note():
    body = "a" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    result = run(FetchUrlTool(httpx.MockTransport(handler)), url="https://example.com/big",
                 max_bytes=40)
    assert result.ok
    lines = result.output.splitlines()
    assert lines[0] == "a" * 40
    # httpx sets Content-Length on the mock response: exact remaining count.
    assert lines[1] == "... [truncated: 60 more bytes]"
    assert result.data["truncated"] is True
    assert result.data["bytes"] == 40


def test_fetch_rejects_non_http_schemes_without_touching_the_network():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="never")

    tool = FetchUrlTool(httpx.MockTransport(handler))
    for url in ("file:///etc/passwd", "ftp://host/file", "gopher://hole", "notaurl"):
        result = run(tool, url=url)
        assert not result.ok
        assert "http" in result.error
    assert calls == []  # scheme check happens before any request is built


def test_fetch_timeout_returns_error_not_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    result = run(FetchUrlTool(httpx.MockTransport(handler)), url="https://slow.example.com/")
    assert not result.ok
    assert "ConnectTimeout" in result.error


def test_fetch_network_error_returns_error_not_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated refused connection")

    result = run(FetchUrlTool(httpx.MockTransport(handler)), url="http://down.example.com/")
    assert not result.ok
    assert "ConnectError" in result.error


def test_fetch_http_error_status_is_ok_false_with_body_preserved():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not here")

    result = run(FetchUrlTool(httpx.MockTransport(handler)), url="https://example.com/missing")
    assert not result.ok
    assert "HTTP 404" in result.error
    assert "not here" in result.output  # body preserved, like shell's nonzero-exit convention
    assert result.data["status"] == 404


def test_fetch_argument_validation():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        return httpx.Response(200, text="x")

    tool = FetchUrlTool(httpx.MockTransport(handler))
    assert not run(tool).ok  # url missing
    assert not run(tool, url="https://e.com/", max_bytes=0).ok
    assert not run(tool, url="https://e.com/", max_bytes="lots").ok
    assert not run(tool, url="http://").ok  # no host


def test_fetch_max_bytes_is_clamped_to_absolute_cap():
    served = MAX_FETCH_BYTES + 10

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"b" * served)

    result = run(FetchUrlTool(httpx.MockTransport(handler)), url="https://example.com/huge",
                 max_bytes=MAX_FETCH_BYTES * 10)
    assert result.ok
    assert result.data["bytes"] == MAX_FETCH_BYTES
    assert result.data["truncated"] is True
    assert "[truncated: 10 more bytes]" in result.output


def test_fetch_default_max_bytes_applies():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"c" * (DEFAULT_MAX_BYTES + 7))

    result = run(FetchUrlTool(httpx.MockTransport(handler)), url="https://example.com/d")
    assert result.ok
    assert result.data["bytes"] == DEFAULT_MAX_BYTES
    assert "[truncated: 7 more bytes]" in result.output


def test_fetch_empty_body_is_reported_honestly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    result = run(FetchUrlTool(httpx.MockTransport(handler)), url="https://example.com/none")
    assert result.ok
    assert result.output == "(empty response body)"


# --- plugin registration (MS-5): after builtins, builtins win -------------------


def _plugin_tool(tool_name: str):
    from ironcore.tools.base import Tool, ToolResult

    class _T(Tool):
        name = tool_name
        description = f"plugin tool. Example: {tool_name}()"
        risk = ToolRisk.READ
        parameters = {"type": "object", "properties": {}, "required": []}

        async def run(self, **kwargs):
            return ToolResult(ok=True, output="plugin ran")

    return _T()


def test_plugin_tools_append_after_builtins_and_builtins_win(tmp_path):
    from ironcore.plugins import LoadedPlugins

    extra = _plugin_tool("extra_tool")
    shadow = _plugin_tool("read_file")
    lp = LoadedPlugins(tools=[extra, shadow])
    registry = build_default_registry(Settings(), tmp_path, plugins=lp)
    assert registry.get("extra_tool") is extra
    assert registry.get("read_file") is not shadow  # the builtin kept its slot
    assert {t.name for t in registry.all()} == LOCAL_TOOLS | {"extra_tool"}
    assert [(s.name, "built-ins win" in s.reason) for s in lp.skipped] == [("read_file", True)]


def test_plugin_edit_formats_reach_edit_file_spec(tmp_path):
    from ironcore.plugins import LoadedPlugins
    from ironcore.tools.patch import PatchResult

    lp = LoadedPlugins(edit_formats={"rot13": lambda o, e: PatchResult(ok=True, new_text=o)})
    registry = build_default_registry(Settings(), tmp_path, plugins=lp)
    enum = registry.get("edit_file").parameters["properties"]["format"]["enum"]
    assert enum == ["unified_diff", "search_replace", "whole_file", "rot13"]
