"""End-to-end proof for instant-on profiling.

Drives the REAL seed (no stubs) against a mock Ollama endpoint: `/api/show`
for the context window + a `/v1/chat/completions` that accepts a tools spec for
capability detection. Proves the ~1-second seed produces a *usable* profile
from introspection alone — native tool-calling and the model's real window,
not the 4k text floor — that the engine immediately adapts to, and that the
background probe refines it without losing the introspected context.
"""

from __future__ import annotations

import asyncio

import httpx

from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.runner import render_report_card, run_probes
from ironcore.envelope.seed import seed_profile
from ironcore.providers.mock import MockProvider
from ironcore.providers.ollama import OllamaProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry as build_tools

ADVERTISED = 32768
NUM_CTX = 8192  # the server's pinned window — the honest ceiling


def _ollama_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/version":
        return httpx.Response(200, json={"version": "0.5.0"})
    if path == "/api/show":
        return httpx.Response(
            200,
            json={
                "details": {"family": "llama", "quantization_level": "Q4_K_M"},
                "model_info": {"llama.context_length": ADVERTISED},
                "parameters": f"num_ctx {NUM_CTX}\nstop <eot>",
            },
        )
    if path == "/v1/chat/completions":
        # accept the tools spec (2xx) -> detect reports native_tools = True
        return httpx.Response(
            200,
            json={"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]},
        )
    if path == "/v1/models":
        return httpx.Response(200, json={"data": [{"id": "qwen3-coder:30b"}]})
    return httpx.Response(404, json={"error": "not found"})


def _capable_ollama() -> tuple[OllamaProvider, httpx.MockTransport]:
    transport = httpx.MockTransport(_ollama_handler)
    provider = OllamaProvider(
        "http://localhost:11434/v1", model="qwen3-coder:30b", transport=transport
    )
    return provider, transport


# --------------------------------------------------------------------------- #
# 1. The instant seed produces a USABLE, HONEST profile from introspection
# --------------------------------------------------------------------------- #


def test_seed_makes_a_capable_model_usable_in_one_shot():
    provider, transport = _capable_ollama()

    async def go():
        try:
            return await seed_profile(provider, model_id="qwen3-coder:30b", transport=transport)
        finally:
            await provider.close()

    seed = asyncio.run(go())

    # usable: native tool-calling + a real window, not the 4k text floor
    assert seed.recommended_tool_protocol() == "native"
    assert seed.recommended_edit_format() == "search_replace"
    assert seed.context_window == ADVERTISED
    assert seed.honest_context == NUM_CTX  # the server's real ceiling, honest
    # honestly labelled provisional, and still "unprobed" so the deep probe runs
    assert seed.source == "seeded"
    assert seed.probed_at is None


def test_seeded_profile_is_labelled_provisional_not_measured():
    provider, transport = _capable_ollama()

    async def go():
        try:
            return await seed_profile(provider, model_id="qwen3-coder:30b", transport=transport)
        finally:
            await provider.close()

    card = render_report_card(asyncio.run(go()))
    assert card.isascii()  # Windows-console safe
    lower = card.lower()
    assert "seeded" in lower or "provisional" in lower
    assert "measured" not in lower.split("verdict")[0]  # the Source line isn't a measurement


# --------------------------------------------------------------------------- #
# 2. The engine immediately adapts to the seed
# --------------------------------------------------------------------------- #


def test_engine_uses_the_seed_native_path_and_edit_format(tmp_path):
    provider, transport = _capable_ollama()

    async def go():
        try:
            return await seed_profile(provider, model_id="qwen3-coder:30b", transport=transport)
        finally:
            await provider.close()

    seed = asyncio.run(go())
    settings = Settings()
    engine = TurnEngine(
        MockProvider([]), build_tools(settings, tmp_path), settings, seed, Mode.AUTO,
        workspace=tmp_path, snapshots=None,
    )
    # the seed drives native tool-calling (not the text floor) and steers edits
    assert engine.profile.recommended_tool_protocol() == "native"
    assert "search_replace" in engine._system_prompt(text_protocol=False)
    # the composer budgets against the introspected honest window, not 4096
    assert engine.profile.honest_context == NUM_CTX


# --------------------------------------------------------------------------- #
# 3. The background probe refines the seed without losing introspected context
# --------------------------------------------------------------------------- #


class _FailingProbe:
    id = "FAIL"
    title = "always fails"
    targets = ("tool_protocols.native",)

    async def run(self, provider):  # noqa: ANN001
        raise RuntimeError("endpoint hiccup")


def test_deep_probe_refines_the_seed_and_keeps_the_window():
    seed = CapabilityProfile(
        model_id="qwen3-coder:30b",
        source="seeded",
        context_window=ADVERTISED,
        honest_context=NUM_CTX,
        tool_protocols={"native": 0.95},
    )
    before = seed.model_dump_json()

    refined = asyncio.run(
        run_probes(
            MockProvider([]),
            [_FailingProbe()],
            model_id="qwen3-coder:30b",
            base=seed,
            probed_at="2026-07-16T00:00:00Z",
        )
    )
    # introspected context survives a probe failure (base-refine, not replace)
    assert refined.honest_context == NUM_CTX
    assert refined.context_window == ADVERTISED
    assert refined.source == "probed"  # now measured
    assert refined.tool_protocols.get("native") == 0.0  # the failed measurement floored it
    assert seed.model_dump_json() == before  # base not mutated
