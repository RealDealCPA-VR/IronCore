"""Instant-on seed (Wave 1 seed core).

``seed_profile`` turns cheap endpoint introspection (Ollama ``/api/show`` +
``detect()``) into a usable-but-provisional CapabilityProfile in ~1s, so the
first turn runs with the model's real window and native tool-calling instead of
the 4k text floor. These tests pin: the seed is usable (native + real context),
honest (never past the server's num_ctx ceiling), resilient (each introspection
failure degrades to floor, never raises), that ``base``-refine carries a seeded
field through a probe failure, and that ``source`` roundtrips.

Everything runs against httpx.MockTransport; async pattern is asyncio.run
(pytest-asyncio is not a dependency of this repo). The SAME MockTransport is
handed to the provider AND to ``seed_profile(transport=...)`` so both
``show_model`` and ``detect`` hit the one fake server.
"""

import asyncio

import httpx

from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.runner import ProbeResult, run_probes
from ironcore.envelope.seed import seed_profile
from ironcore.providers.mock import MockProvider
from ironcore.providers.ollama import OllamaProvider
from ironcore.providers.openai_compat import OpenAICompatProvider

BASE = "http://testserver/v1"

# /api/show payload: advertised window 131072, server-pinned num_ctx 8192
SHOW_JSON = {
    "parameters": 'num_ctx                    8192\nstop                       "<|eot_id|>"',
    "details": {"format": "gguf", "family": "llama", "quantization_level": "Q4_K_M"},
    "model_info": {
        "general.architecture": "llama",
        "llama.context_length": 131072,
        "llama.embedding_length": 4096,
    },
}


def _chat_ok():
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "pong"},
                    "finish_reason": "stop",
                }
            ]
        },
    )


def _ollama_handler(*, show=SHOW_JSON, reject_tools=False):
    """A fake Ollama server: /api/version (hint), /api/show (context), and
    /v1/chat/completions (detect's knob probes). ``show=None`` 404s /api/show;
    ``reject_tools`` 400s the native-tools probe only."""

    def handler(request):
        path = request.url.path
        method = request.method
        if method == "GET" and path == "/api/version":
            return httpx.Response(200, json={"version": "0.9.9"})
        if method == "GET" and path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3:8b"}]})
        if method == "POST" and path == "/api/show":
            if show is None:
                return httpx.Response(404, text="unknown route")
            return httpx.Response(200, json=show)
        if method == "POST" and path == "/v1/chat/completions":
            body = request.content.decode()
            if reject_tools and '"tools"' in body:
                return httpx.Response(400, json={"error": "unknown parameter: tools"})
            return _chat_ok()
        return httpx.Response(404, text="unhandled")

    return handler


def _plain_handler():
    """A non-Ollama OpenAI-compatible server: no /api/*; chat 200 so detect
    still finds native tools; /v1/models present for the hint sniff."""

    def handler(request):
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(404, text="not found")
        if path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "m"}]})
        if request.method == "POST" and path == "/v1/chat/completions":
            return _chat_ok()
        return httpx.Response(404, text="unhandled")

    return handler


async def _nosleep(_delay):
    return None


def _ollama(handler, *, api_key="sk-seed-test"):
    transport = httpx.MockTransport(handler)
    provider = OllamaProvider(
        BASE, api_key=api_key, model="llama3:8b", transport=transport, sleep=_nosleep
    )
    return provider, transport


def _seed(provider, transport, *, model_id="llama3:8b"):
    async def go():
        try:
            return await seed_profile(provider, model_id=model_id, transport=transport)
        finally:
            await provider.close()

    return asyncio.run(go())


# --- capable Ollama endpoint: usable + honest seed ---------------------------


def test_seed_capable_ollama_is_usable_and_honest():
    provider, transport = _ollama(_ollama_handler())
    profile = _seed(provider, transport)

    assert profile.context_window == 131072  # advertised window
    assert profile.honest_context == 8192  # the server's num_ctx ceiling, not the window
    assert profile.recommended_tool_protocol() == "native"  # seeded above threshold
    assert profile.recommended_edit_format() == "search_replace"  # safe middle rung
    assert profile.source == "seeded"
    assert profile.probed_at is None  # still unprobed → the deep probe still runs


# --- show_model failure: context degrades to floor, no raise -----------------


def test_seed_when_show_model_fails_keeps_context_default():
    provider, transport = _ollama(_ollama_handler(show=None))  # /api/show 404s
    profile = _seed(provider, transport)

    assert profile.context_window == 8192  # floor defaults, untouched
    assert profile.honest_context == 4096
    # detect still succeeded (chat 200) → capabilities are still seeded
    assert profile.recommended_tool_protocol() == "native"
    assert profile.source == "seeded"


# --- detect finds no native tools: tool/edit floors, context still seeded ----


def test_seed_without_native_tools_falls_to_text_floor():
    provider, transport = _ollama(_ollama_handler(reject_tools=True))
    profile = _seed(provider, transport)

    assert profile.tool_protocols == {}
    assert profile.recommended_tool_protocol() == "text_protocol"  # floor
    assert profile.recommended_edit_format() == "whole_file"  # floor
    assert profile.honest_context == 8192  # context still seeded from show_model
    assert profile.source == "seeded"


# --- non-Ollama provider: no show_model, capabilities from detect ------------


def test_seed_non_ollama_provider_uses_detect_only():
    transport = httpx.MockTransport(_plain_handler())
    provider = OpenAICompatProvider(
        BASE, api_key="sk-seed-test", model="m", transport=transport, sleep=_nosleep
    )

    async def go():
        try:
            return await seed_profile(provider, model_id="m", transport=transport)
        finally:
            await provider.close()

    profile = asyncio.run(go())
    assert not hasattr(provider, "show_model")
    assert profile.context_window == 8192  # no show_model → context defaults
    assert profile.honest_context == 4096
    assert profile.recommended_tool_protocol() == "native"  # detect found native tools
    assert profile.source == "seeded"


# --- base-refine: a seeded honest_context survives a probe failure -----------


class _CtxRaiser:
    """A context probe that raises — its target must be left at the base value,
    not degraded (a failed measurement never invents a smaller honest_context)."""

    id = "CTX"
    title = "context probe that raises"
    targets = ("honest_context",)

    async def run(self, provider):
        raise ValueError("no needles found")


def test_base_refine_preserves_seeded_honest_context():
    seed = CapabilityProfile(model_id="m", honest_context=32768, source="seeded")
    profile = asyncio.run(
        run_probes(MockProvider([]), [_CtxRaiser()], model_id="m", base=seed, probed_at="t")
    )
    assert profile.honest_context == 32768  # the seed survived the probe failure
    assert profile.source == "probed"  # but it is now a measured profile


class _NativeProbe:
    """A tiny probe that measures native as reliable — proves refine also merges
    real measurements over a seed."""

    id = "TOOL"
    title = "native tool form"
    targets = ("tool_protocols.native",)

    async def run(self, provider):
        return ProbeResult(self.id, {"tool_protocols.native": 0.97})


def test_base_refine_merges_measurement_over_seed():
    seed = CapabilityProfile(
        model_id="m", honest_context=32768, tool_protocols={"native": 0.95}, source="seeded"
    )
    profile = asyncio.run(
        run_probes(MockProvider([]), [_NativeProbe()], model_id="m", base=seed, probed_at="t")
    )
    assert profile.honest_context == 32768  # untouched field carried from the seed
    assert profile.tool_protocols["native"] == 0.97  # measurement won
    assert profile.source == "probed"


# --- vision seeding (MS-6) ---------------------------------------------------


def test_seed_sets_vision_from_show_capabilities():
    show = dict(SHOW_JSON, capabilities=["completion", "vision"])
    provider, transport = _ollama(_ollama_handler(show=show))
    profile = _seed(provider, transport)
    assert profile.vision is True


def test_seed_without_vision_capability_stays_false():
    provider, transport = _ollama(_ollama_handler())  # SHOW_JSON has no capabilities
    profile = _seed(provider, transport)
    assert profile.vision is False


def test_seed_show_failure_keeps_vision_floor_default():
    provider, transport = _ollama(_ollama_handler(show=None))
    profile = _seed(provider, transport)
    assert profile.vision is False


def test_base_refine_preserves_seeded_vision():
    # run_probes deep-copies the base and merges only dotted-path scores, so a
    # seeded vision=True survives deep-probe refinement untouched.
    seed = CapabilityProfile(model_id="m", vision=True, source="seeded")
    profile = asyncio.run(
        run_probes(
            MockProvider([]), [_NativeProbe()], model_id="m", base=seed, probed_at="t"
        )
    )
    assert profile.vision is True
    assert profile.source == "probed"


def test_legacy_profile_json_loads_vision_false():
    legacy = {"model_id": "m"}  # an envelope JSON written before the field existed
    assert CapabilityProfile.model_validate(legacy).vision is False


# --- source roundtrips through save/load -------------------------------------


def test_source_roundtrips_through_save_load(tmp_path):
    assert CapabilityProfile(model_id="m").source == "default"  # backward-compatible default
    profile = CapabilityProfile(model_id="m", source="seeded")
    profile.save(tmp_path)
    loaded = CapabilityProfile.load(tmp_path, "m")
    assert loaded is not None
    assert loaded.source == "seeded"
    assert loaded == profile
