"""IC-203 pins: /api/tags discovery -> ModelInfo, /api/show -> ModelDetails,
keep_alive injection into chat bodies, list_models fallback to /v1/models,
api_root derivation, and graceful degradation on non-Ollama endpoints.

Everything runs against httpx.MockTransport with an injected fake sleep;
async pattern is asyncio.run (pytest-asyncio is not a dependency).
The /api/* URLs asserted here prove the root derivation: /api is a sibling
of /v1 at the server root, never base_url + "/api/...".
"""

import asyncio
import json

import httpx
import pytest

from ironcore.providers.base import Message
from ironcore.providers.ollama import ModelDetails, ModelInfo, OllamaProvider
from ironcore.providers.openai_compat import ProviderError

BASE = "http://testserver/v1"
ROOT = "http://testserver"

TAGS_JSON = {
    "models": [
        {
            "name": "llama3:8b",
            "model": "llama3:8b",
            "size": 4661224676,
            "modified_at": "2026-07-01T12:00:00.000000-07:00",
            "digest": "abc123",
            "details": {"family": "llama", "quantization_level": "Q4_0"},
        },
        # sparse entry: discovery must tolerate missing size/modified_at
        {"name": "qwen2.5-coder:7b"},
        # garbage entries: skipped, never a crash
        {"size": 123},
        "not-a-dict",
    ]
}

SHOW_JSON = {
    "parameters": 'num_ctx                    8192\nstop                       "<|eot_id|>"',
    "details": {"format": "gguf", "family": "llama", "quantization_level": "Q4_K_M"},
    "model_info": {
        "general.architecture": "llama",
        "llama.context_length": 131072,
        "llama.embedding_length": 4096,
    },
}


def make_provider(handler, *, api_key="sk-unit-test", **kwargs):
    """OllamaProvider on a MockTransport, plus the list its fake sleep fills."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    provider = OllamaProvider(
        BASE + "/",  # trailing slash on purpose: normalization is inherited
        api_key=api_key,
        model="llama3:8b",
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
        **kwargs,
    )
    return provider, sleeps


def run_closing(provider, coro):
    """await one provider coroutine, always closing the client."""

    async def go():
        try:
            return await coro
        finally:
            await provider.close()

    return asyncio.run(go())


def completion_json(content="hello"):
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ]
    }


def sse_bytes(*chunks, done=True):
    parts = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    if done:
        parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()


# --- api_root ----------------------------------------------------------------


def test_api_root_strips_trailing_v1_variants():
    cases = {
        "http://localhost:11434/v1": "http://localhost:11434",
        "http://localhost:11434/v1/": "http://localhost:11434",
        "http://localhost:11434": "http://localhost:11434",
        "http://proxy.example/openai": "http://proxy.example/openai",  # no /v1: unchanged
        "https://host/v1/v1": "https://host/v1",  # only the TRAILING /v1 strips
    }
    for base, expected in cases.items():
        provider = OllamaProvider(base)
        try:
            assert provider.api_root == expected, base
        finally:
            asyncio.run(provider.close())


# --- /api/tags discovery -----------------------------------------------------


def test_discover_models_hits_api_tags_at_server_root():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json=TAGS_JSON)

    provider, _ = make_provider(handler)
    models = run_closing(provider, provider.discover_models())

    assert seen["method"] == "GET"
    assert seen["url"] == ROOT + "/api/tags"  # /v1 stripped, not concatenated
    assert models == [
        ModelInfo(
            name="llama3:8b",
            size_bytes=4661224676,
            modified_at="2026-07-01T12:00:00.000000-07:00",
        ),
        ModelInfo(name="qwen2.5-coder:7b", size_bytes=None, modified_at=None),
    ]


def test_discover_models_retries_transient_500_then_succeeds():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503, text="loading")
        return httpx.Response(200, json=TAGS_JSON)

    provider, sleeps = make_provider(handler)
    models = run_closing(provider, provider.discover_models())
    assert [m.name for m in models] == ["llama3:8b", "qwen2.5-coder:7b"]
    assert len(attempts) == 2
    assert len(sleeps) == 1


def test_discover_models_404_raises_with_ollama_hint():
    provider, _ = make_provider(lambda request: httpx.Response(404, text="no such route"))
    with pytest.raises(ProviderError, match=r"not an Ollama endpoint\?"):
        run_closing(provider, provider.discover_models())


def test_discover_models_connect_failure_raises_with_ollama_hint():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    provider, sleeps = make_provider(handler)
    with pytest.raises(ProviderError, match=r"not an Ollama endpoint\?"):
        run_closing(provider, provider.discover_models())
    assert len(sleeps) == 2  # transport errors still retried before giving up


def test_discover_models_non_json_body_raises_with_ollama_hint():
    provider, _ = make_provider(lambda request: httpx.Response(200, text="<html>lm studio</html>"))
    with pytest.raises(ProviderError, match=r"not an Ollama endpoint\?"):
        run_closing(provider, provider.discover_models())


def test_api_error_bodies_are_redacted():
    key = "sk-SUPER$ecret.key+123"

    def handler(request):
        # hostile/echoing server reflects the auth header into the error body
        return httpx.Response(404, text=f"bad key: {request.headers['authorization']}")

    provider, _ = make_provider(handler, api_key=key)
    with pytest.raises(ProviderError) as excinfo:
        run_closing(provider, provider.discover_models())
    assert key not in str(excinfo.value)
    assert "bad key" in str(excinfo.value)  # the useful part survives redaction


# --- list_models preference + fallback ---------------------------------------


def test_list_models_prefers_api_tags_names():
    hits = {"tags": 0, "models": 0}

    def handler(request):
        if request.url.path == "/api/tags":
            hits["tags"] += 1
            return httpx.Response(200, json=TAGS_JSON)
        hits["models"] += 1
        return httpx.Response(200, json={"data": [{"id": "should-not-be-used"}]})

    provider, _ = make_provider(handler)
    names = run_closing(provider, provider.list_models())
    assert names == ["llama3:8b", "qwen2.5-coder:7b"]
    assert hits == {"tags": 1, "models": 0}  # the OpenAI path was never consulted


def test_list_models_falls_back_to_openai_models_on_404():
    hits = {"tags": 0, "models": 0}

    def handler(request):
        if request.url.path == "/api/tags":
            hits["tags"] += 1
            return httpx.Response(404, text="not found")
        hits["models"] += 1
        assert str(request.url) == BASE + "/models"  # fallback stays under /v1
        return httpx.Response(200, json={"data": [{"id": "gpt-oss:20b"}, {"id": "phi4"}]})

    provider, sleeps = make_provider(handler)
    names = run_closing(provider, provider.list_models())
    assert names == ["gpt-oss:20b", "phi4"]
    assert hits == {"tags": 1, "models": 1}  # 404 never retries
    assert sleeps == []


def test_list_models_falls_back_on_connection_failure():
    def handler(request):
        if request.url.path == "/api/tags":
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"data": [{"id": "vllm-model"}]})

    provider, _ = make_provider(handler)
    assert run_closing(provider, provider.list_models()) == ["vllm-model"]


# --- /api/show introspection -------------------------------------------------


def test_show_model_parses_context_quant_family_and_num_ctx():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=SHOW_JSON)

    provider, _ = make_provider(handler)
    details = run_closing(provider, provider.show_model("llama3:8b"))

    assert seen["method"] == "POST"
    assert seen["url"] == ROOT + "/api/show"
    assert seen["body"] == {"model": "llama3:8b"}
    assert details == ModelDetails(
        context_length=131072,  # hunted from nested "llama.context_length"
        quantization="Q4_K_M",
        family="llama",
        num_ctx_configured=8192,  # parsed out of the parameters text blob
    )


def test_show_model_missing_fields_stay_none():
    provider, _ = make_provider(lambda request: httpx.Response(200, json={"modelfile": "FROM x"}))
    details = run_closing(provider, provider.show_model("mystery"))
    assert details == ModelDetails(
        context_length=None, quantization=None, family=None, num_ctx_configured=None
    )


def test_show_model_failure_raises_provider_error_with_hint():
    provider, _ = make_provider(lambda request: httpx.Response(404, text="unknown route"))
    with pytest.raises(ProviderError, match=r"not an Ollama endpoint\?"):
        run_closing(provider, provider.show_model("llama3:8b"))


# --- keep_alive injection ----------------------------------------------------


def test_complete_injects_default_keep_alive():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=completion_json("hi"))

    provider, _ = make_provider(handler)
    result = run_closing(provider, provider.complete([Message(role="user", content="hi")]))
    assert result.message.content == "hi"
    assert seen["body"]["keep_alive"] == "10m"


def test_complete_honors_custom_keep_alive():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=completion_json())

    provider, _ = make_provider(handler, keep_alive="30m")
    run_closing(provider, provider.complete([Message(role="user", content="hi")]))
    assert seen["body"]["keep_alive"] == "30m"


def test_keep_alive_none_is_omitted_from_body():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=completion_json())

    provider, _ = make_provider(handler, keep_alive=None)
    run_closing(provider, provider.complete([Message(role="user", content="hi")]))
    assert "keep_alive" not in seen["body"]


def test_stream_injects_keep_alive_too():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode())
        chunk = {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200, content=sse_bytes(chunk), headers={"content-type": "text/event-stream"}
        )

    provider, _ = make_provider(handler)

    async def go():
        try:
            return [e async for e in provider.stream([Message(role="user", content="hi")])]
        finally:
            await provider.close()

    events = asyncio.run(go())
    assert seen["body"]["stream"] is True
    assert seen["body"]["keep_alive"] == "10m"
    assert [e.kind for e in events] == ["text", "done"]


# --- check_context mismatch warning ------------------------------------------


def show_handler(payload):
    def handler(request):
        return httpx.Response(200, json=payload)

    return handler


def test_check_context_warns_when_model_window_too_small():
    payload = {"model_info": {"llama.context_length": 8192}}
    provider, _ = make_provider(show_handler(payload))
    warning = run_closing(provider, provider.check_context("llama3:8b", 32768))
    assert warning is not None
    assert "llama3:8b" in warning
    assert "8192" in warning and "32768" in warning


def test_check_context_warns_when_configured_num_ctx_too_small():
    payload = {
        "model_info": {"llama.context_length": 131072},
        "parameters": "num_ctx    2048",
    }
    provider, _ = make_provider(show_handler(payload))
    warning = run_closing(provider, provider.check_context("llama3:8b", 8192))
    assert warning is not None
    assert "num_ctx" in warning and "2048" in warning


def test_check_context_none_when_capacity_is_enough():
    provider, _ = make_provider(show_handler(SHOW_JSON))  # window 131072, num_ctx 8192
    assert run_closing(provider, provider.check_context("llama3:8b", 4096)) is None


def test_check_context_none_when_show_reports_nothing():
    # unknowns are not warnings: the envelope handles unprobed models
    provider, _ = make_provider(show_handler({}))
    assert run_closing(provider, provider.check_context("mystery", 128000)) is None
