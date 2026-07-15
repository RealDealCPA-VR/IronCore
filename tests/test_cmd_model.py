"""/model (IC-801): switch the configured model, or schedule a model listing."""

import asyncio

from ironcore.commands.base import CommandContext
from ironcore.commands.modelcmd import _cmd_model
from ironcore.config.settings import Settings
from ironcore.providers.mock import MockProvider
from ironcore.providers.openai_compat import ProviderError


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
