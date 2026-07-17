"""/model: IC-801 (settings switch + listing) and MS-2 (LIVE swaps).

MS-2 cases drive a real TurnEngine + a duck-typed for_model registry, all
offline: cache HIT repoints instantly; cache MISS floors immediately and
queues exactly one background seed→deepen task (drained re-entrantly — the
seed step schedules the deep probe itself); busy / headless / closed-registry
paths fall back without touching the engine.
"""

import asyncio

from ironcore.commands.base import CommandContext
from ironcore.commands.modelcmd import _cmd_model
from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.envelope.probe_tools import ToolFormProbe
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.providers.openai_compat import ProviderError
from ironcore.safety.modes import Mode
from ironcore.tools.base import ToolRegistry


class FakeRegistry:
    def __init__(self, provider, *, closed=False):
        self._provider = provider
        self._closed = closed

    @property
    def default(self):
        if self._closed:
            raise RuntimeError("closed")
        return self._provider


class RaisingProvider:
    async def list_models(self):
        raise ProviderError("endpoint down")


def _ctx(**extra):
    ctx = CommandContext(settings=Settings())
    ctx.extra.update(extra)
    return ctx


def _sync_schedule():
    captured: list[str] = []

    def schedule(coro):
        captured.append(asyncio.run(coro))

    return schedule, captured


def test_switch_updates_settings_and_advises_probe():
    ctx = _ctx()
    was = ctx.settings.provider.model
    out = _cmd_model(ctx, "qwen3:8b")
    assert ctx.settings.provider.model == "qwen3:8b"
    assert "qwen3:8b" in out and was in out
    assert "probe" in out.lower()


def test_list_without_registry_reports_configured_model():
    schedule, _ = _sync_schedule()
    ctx = _ctx(schedule=schedule)  # no provider_registry
    out = _cmd_model(ctx, "")
    assert ctx.settings.provider.model in out
    assert "live endpoint" in out


def test_list_schedules_and_marks_current():
    schedule, captured = _sync_schedule()
    ctx = _ctx(provider_registry=FakeRegistry(MockProvider()), schedule=schedule)
    ctx.settings.provider.model = "mock-model"
    ack = _cmd_model(ctx, "")
    assert "Listing models" in ack
    assert captured and "mock-model" in captured[0]
    assert "(current)" in captured[0]


def test_list_reports_model_not_served():
    schedule, captured = _sync_schedule()
    ctx = _ctx(provider_registry=FakeRegistry(MockProvider()), schedule=schedule)
    ctx.settings.provider.model = "not-there"
    _cmd_model(ctx, "")
    assert "not in the list" in captured[0]


def test_list_surfaces_provider_error():
    schedule, captured = _sync_schedule()
    ctx = _ctx(provider_registry=FakeRegistry(RaisingProvider()), schedule=schedule)
    _cmd_model(ctx, "")
    assert captured and "endpoint down" in captured[0]


def test_list_with_closed_registry_reports_cleanly():
    schedule, _ = _sync_schedule()
    ctx = _ctx(provider_registry=FakeRegistry(MockProvider(), closed=True), schedule=schedule)
    out = _cmd_model(ctx, "")
    assert "closed" in out.lower()


# --------------------------------------------------------------------------- #
# MS-2: live swaps
# --------------------------------------------------------------------------- #

WEATHER = {"city": "Paris", "units": "celsius"}


def _native_reply() -> CompletionResult:
    return CompletionResult(
        message=Message(
            role="assistant", tool_calls=[ToolCall(id="n", name="get_weather", arguments=WEATHER)]
        )
    )


def _text(s: str) -> CompletionResult:
    return CompletionResult(message=Message(role="assistant", content=s))


def _capable_script() -> list:
    # ToolFormProbe(trials=1) issues exactly 3 calls: native, strict_json, text
    return [_native_reply(), _text("no json"), _text("no block")]


def _engine(tmp_path, provider=None, model="mock-model") -> TurnEngine:
    settings = Settings()
    settings.provider.model = model
    return TurnEngine(
        provider if provider is not None else MockProvider([]),
        ToolRegistry(),
        settings,
        CapabilityProfile(model_id=model),
        Mode.MANUAL,
        workspace=tmp_path,
        snapshots=None,
    )


def _measured_profile(name: str) -> CapabilityProfile:
    return CapabilityProfile(
        model_id=name,
        probed_at="2026-07-16T00:00:00Z",
        source="probed",
        tool_protocols={"native": 1.0},
    )


class SwapRegistry:
    """for_model seam: one provider per model, requests recorded."""

    def __init__(self):
        self.providers: dict[str, MockProvider] = {}
        self.requested: list[str] = []

    def for_model(self, model):
        self.requested.append(model)
        return self.providers.setdefault(model, MockProvider([]))


class ClosedSwapRegistry:
    def for_model(self, model):
        raise RuntimeError("ProviderRegistry is closed; build a new one from settings")


class BusyApp:
    def _turn_running(self):
        return True


def _queue_schedule():
    queue: list = []
    return queue.append, queue


def test_live_switch_cache_hit_repoints_instantly(tmp_path):
    env = tmp_path / "env"
    _measured_profile("qwen3:8b").save(env)
    engine = _engine(tmp_path)
    registry = SwapRegistry()
    schedule, queue = _queue_schedule()
    ctx = _ctx(engine=engine, provider_registry=registry, envelope_dir=env, schedule=schedule)
    out = _cmd_model(ctx, "qwen3:8b")
    assert ctx.settings.provider.model == "qwen3:8b"
    # the RUNNING session is re-pointed: provider + measured profile + author
    assert engine.provider is registry.providers["qwen3:8b"]
    assert engine.profile.probed_at is not None
    assert engine.profile.model_id == "qwen3:8b"
    assert engine.handoff_author.endswith("qwen3:8b")
    assert queue == []  # instant — NO background work on a hit
    assert "cache" in out and "measured" in out


def test_live_switch_cache_miss_floors_now_and_deepens_in_background(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "ironcore.envelope.suite.default_probe_suite", lambda: [ToolFormProbe(trials=1)]
    )
    env = tmp_path / "env"
    engine = _engine(tmp_path)
    registry = SwapRegistry()
    registry.providers["qwen3:8b"] = MockProvider(_capable_script())
    schedule, queue = _queue_schedule()
    ctx = _ctx(engine=engine, provider_registry=registry, envelope_dir=env, schedule=schedule)
    out = _cmd_model(ctx, "qwen3:8b")
    # usable IMMEDIATELY: floor profile with the new model id, new provider
    assert engine.provider is registry.providers["qwen3:8b"]
    assert engine.profile.model_id == "qwen3:8b"
    assert engine.profile.probed_at is None
    assert len(queue) == 1  # exactly one background task queued
    assert "background" in out
    # drain re-entrantly: the seed step schedules the deep probe as its own task
    while queue:
        asyncio.run(queue.pop(0))
    assert engine.profile.source == "probed"
    assert engine.profile.recommended_tool_protocol() == "native"
    # ... and the cache now REMEMBERS this model for the next swap
    assert CapabilityProfile.load(env, "qwen3:8b") is not None


def test_switch_refused_while_a_turn_runs(tmp_path):
    engine = _engine(tmp_path)
    provider_before = engine.provider
    registry = SwapRegistry()
    schedule, queue = _queue_schedule()
    ctx = _ctx(
        app=BusyApp(), engine=engine, provider_registry=registry,
        envelope_dir=tmp_path / "env", schedule=schedule,
    )
    was = ctx.settings.provider.model
    out = _cmd_model(ctx, "qwen3:8b")
    assert "Esc" in out
    # NOTHING moved: settings, provider, profile, and no scheduled work
    assert ctx.settings.provider.model == was
    assert engine.provider is provider_before
    assert engine.profile.model_id == "mock-model"
    assert queue == [] and registry.requested == []


def test_switch_with_engine_but_no_registry_advises_probe(tmp_path):
    engine = _engine(tmp_path)
    ctx = _ctx(engine=engine)  # no provider_registry → settings-only fallback
    out = _cmd_model(ctx, "qwen3:8b")
    assert ctx.settings.provider.model == "qwen3:8b"
    assert "probe" in out.lower()
    assert engine.profile.model_id == "mock-model"  # engine untouched


def test_switch_with_closed_registry_falls_back_to_settings_only(tmp_path):
    engine = _engine(tmp_path)
    ctx = _ctx(engine=engine, provider_registry=ClosedSwapRegistry())
    out = _cmd_model(ctx, "qwen3:8b")
    assert ctx.settings.provider.model == "qwen3:8b"
    assert "probe" in out.lower()
    assert engine.profile.model_id == "mock-model"  # engine untouched


def test_switch_to_current_model_is_a_noop(tmp_path):
    engine = _engine(tmp_path)
    registry = SwapRegistry()
    ctx = _ctx(engine=engine, provider_registry=registry)
    ctx.settings.provider.model = "mock-model"
    out = _cmd_model(ctx, "mock-model")
    assert "Already on" in out
    assert registry.requested == []  # no provider built, nothing re-pointed


def test_list_marks_cached_measured_models(tmp_path):
    env = tmp_path / "env"
    _measured_profile("mock-model").save(env)
    schedule, captured = _sync_schedule()
    ctx = _ctx(provider_registry=FakeRegistry(MockProvider()), schedule=schedule,
               envelope_dir=env)
    ctx.settings.provider.model = "mock-model"
    _cmd_model(ctx, "")
    assert "(current)" in captured[0]  # IC-801 wording survives (suffix-only)
    assert "measured" in captured[0]


def test_list_does_not_mark_unmeasured_models(tmp_path):
    schedule, captured = _sync_schedule()
    ctx = _ctx(provider_registry=FakeRegistry(MockProvider()), schedule=schedule,
               envelope_dir=tmp_path / "env")  # empty cache
    ctx.settings.provider.model = "mock-model"
    _cmd_model(ctx, "")
    assert "measured" not in captured[0]
