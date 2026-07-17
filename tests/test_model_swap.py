"""MS-2 live model swaps, end-to-end and offline.

``/model <name>`` through the REAL ``ProviderRegistry`` (an injected counting
factory builds one MockProvider per model): a cache MISS floors the engine
immediately and background-deepens (seed step scheduled, which schedules the
deep probe — drained re-entrantly), the deepen caches the measured profile,
and swapping BACK is a pure cache hit — no scheduled work, the identical
provider instance from the registry's per-model cache, the measured envelope
from disk. The on-disk cache remembers every model you've measured.
"""

from __future__ import annotations

import asyncio

from ironcore.commands.base import CommandContext
from ironcore.commands.modelcmd import _cmd_model
from ironcore.config.settings import ProviderSettings, Settings
from ironcore.core.engine import TurnEngine
from ironcore.envelope.probe_tools import ToolFormProbe
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.providers.registry import ProviderRegistry
from ironcore.safety.modes import Mode
from ironcore.tools.base import ToolRegistry

WEATHER = {"city": "Paris", "units": "celsius"}


def _native_reply() -> CompletionResult:
    return CompletionResult(
        message=Message(
            role="assistant", tool_calls=[ToolCall(id="n", name="get_weather", arguments=WEATHER)]
        )
    )


def _text(s: str) -> CompletionResult:
    return CompletionResult(message=Message(role="assistant", content=s))


def _factory(*, base_url: str = "", api_key: str = "", model: str = "") -> MockProvider:
    """Registry factory seam: a MockProvider per model, each pre-scripted for one
    ToolFormProbe(trials=1) run (3 calls). Deliberately NO ``base_url`` attribute —
    the swap path then skips endpoint seeding (MockProvider is not endpoint-backed)."""
    provider = MockProvider([_native_reply(), _text("no json"), _text("no block")])
    provider.model = model
    return provider


def _drain(queue: list) -> None:
    """Re-entrant-safe: a drained coroutine may schedule more (seed → deepen)."""
    while queue:
        asyncio.run(queue.pop(0))


def test_swap_deepen_cache_then_swap_back_is_a_pure_hit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "ironcore.envelope.suite.default_probe_suite", lambda: [ToolFormProbe(trials=1)]
    )
    env = tmp_path / "env"
    settings = Settings(
        provider=ProviderSettings(
            base_url="http://testserver/v1", api_key="sk-unit-test", model="model-a"
        )
    )
    registry = ProviderRegistry(settings, provider_factory=_factory)
    engine = TurnEngine(
        registry.default,
        ToolRegistry(),
        settings,
        CapabilityProfile(model_id="model-a"),
        Mode.MANUAL,
        workspace=tmp_path,
        snapshots=None,
    )
    queue: list = []
    ctx = CommandContext(settings=settings)
    ctx.extra.update(
        {
            "engine": engine,
            "provider_registry": registry,
            "envelope_dir": env,
            "schedule": queue.append,
        }
    )

    # -- /model model-b: MISS → floor now, deepen in the background ------------
    out = _cmd_model(ctx, "model-b")
    assert "background" in out
    provider_b = engine.provider
    assert provider_b is not registry.default
    assert engine.profile.model_id == "model-b" and engine.profile.probed_at is None
    _drain(queue)
    assert engine.profile.source == "probed"  # deepen hot-swapped the measurement
    assert engine.profile.recommended_tool_protocol() == "native"
    assert CapabilityProfile.load(env, "model-b") is not None  # cached on disk

    # -- back to model-a (its own miss; drain its deepen too) ------------------
    _cmd_model(ctx, "model-a")
    assert engine.provider is registry.default  # the original instance, cached
    _drain(queue)
    assert settings.provider.model == "model-a"
    assert CapabilityProfile.load(env, "model-a") is not None

    # -- /model model-b AGAIN: a PURE cache hit --------------------------------
    out = _cmd_model(ctx, "model-b")
    assert queue == []  # instant: NO background work scheduled
    assert engine.provider is provider_b  # registry cache: one client per model
    assert engine.profile.probed_at is not None  # the measured envelope, from disk
    assert engine.profile.recommended_tool_protocol() == "native"
    assert engine.handoff_author == "ironcore/model-b"
    assert "cache" in out and "measured" in out
