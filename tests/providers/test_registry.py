"""IC-204 pins: from_settings respects settings, unset-role identity fallback,
routed roles as distinct same-endpoint instances, per-model caching, unknown
role -> ValueError listing valid roles, close_all exactly-once idempotency,
transport pass-through.

Fakes count instantiations/closes via the injectable provider_factory; the
default-factory path runs against httpx.MockTransport. Async pattern:
asyncio.run (pytest-asyncio is not a dependency of this repo).
"""

import asyncio
import json

import httpx
import pytest

from ironcore.config.settings import ProviderSettings, RoleModels, Settings
from ironcore.providers.base import Message, Provider
from ironcore.providers.openai_compat import OpenAICompatProvider
from ironcore.providers.registry import VALID_ROLES, ProviderRegistry


class FakeProvider(Provider):
    """Records constructor kwargs and close() calls; never talks to a network."""

    name = "fake"

    def __init__(self, base_url="", api_key="", model="", **kwargs):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.kwargs = kwargs
        self.close_count = 0

    async def complete(self, messages, *, tools=None, sampling=None):
        raise NotImplementedError

    def stream(self, messages, *, tools=None, sampling=None, response_format=None, extra_body=None):
        raise NotImplementedError

    async def list_models(self):
        return [self.model]

    async def close(self):
        self.close_count += 1


def make_settings(*, model="default-model", **roles):
    return Settings(
        provider=ProviderSettings(
            base_url="http://testserver/v1", api_key="sk-unit-test", model=model
        ),
        roles=RoleModels(**roles),
    )


def make_registry(settings):
    """Registry wired to a counting factory; returns (registry, created list)."""
    created: list[FakeProvider] = []

    def factory(**kwargs):
        provider = FakeProvider(**kwargs)
        created.append(provider)
        return provider

    return ProviderRegistry(settings, provider_factory=factory), created


# --- construction ------------------------------------------------------------


def test_valid_roles_match_rolemodels_fields():
    # the registry's static tuple must track config.settings.RoleModels
    assert VALID_ROLES == tuple(RoleModels.model_fields)


def test_from_settings_builds_default_from_provider_settings():
    registry = ProviderRegistry.from_settings(make_settings())
    try:
        default = registry.default
        assert isinstance(default, OpenAICompatProvider)
        assert default.base_url == "http://testserver/v1"
        assert default.api_key == "sk-unit-test"
        assert default.model == "default-model"
    finally:
        asyncio.run(registry.close_all())


def test_from_settings_transport_seam_reaches_the_wire():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["model"] = json.loads(request.content.decode())["model"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ]
            },
        )

    registry = ProviderRegistry.from_settings(
        make_settings(planner="big-model"), transport=httpx.MockTransport(handler)
    )

    async def go():
        try:
            return await registry.for_role("planner").complete(
                [Message(role="user", content="hi")]
            )
        finally:
            await registry.close_all()

    result = asyncio.run(go())
    assert result.message.content == "ok"
    assert seen["url"] == "http://testserver/v1/chat/completions"
    assert seen["model"] == "big-model"  # routed provider sends the ROLE model


def test_factory_receives_settings_values_and_no_transport_when_none():
    registry, created = make_registry(make_settings())
    assert len(created) == 1  # the default, built eagerly
    assert created[0].base_url == "http://testserver/v1"
    assert created[0].api_key == "sk-unit-test"
    assert created[0].model == "default-model"
    assert "transport" not in created[0].kwargs  # omitted so fakes need not accept it


# --- role routing ------------------------------------------------------------


def test_unset_role_returns_the_default_object_not_a_copy():
    registry, created = make_registry(make_settings())  # all roles unset
    for role in VALID_ROLES:
        assert registry.for_role(role) is registry.default
    assert len(created) == 1  # fallback never constructs anything new


def test_set_role_returns_distinct_instance_with_role_model():
    registry, _ = make_registry(make_settings(planner="big-model"))
    planner = registry.for_role("planner")
    assert planner is not registry.default
    assert planner.model == "big-model"
    # same endpoint, many models (SPEC #4.4): base_url/api_key are shared
    assert planner.base_url == registry.default.base_url
    assert planner.api_key == registry.default.api_key
    assert registry.for_role("coder") is registry.default  # unset role still falls back


def test_role_provider_is_cached_one_instantiation_for_two_calls():
    registry, created = make_registry(make_settings(planner="big-model"))
    first = registry.for_role("planner")
    second = registry.for_role("planner")
    assert first is second
    assert len(created) == 2  # default + planner, nothing else


def test_roles_sharing_a_model_share_one_instance():
    registry, created = make_registry(
        make_settings(planner="big-model", verifier="big-model", coder="default-model")
    )
    assert registry.for_role("planner") is registry.for_role("verifier")
    # a role routed to the default's own model reuses the default instance
    assert registry.for_role("coder") is registry.default
    assert len(created) == 2  # default + one shared big-model client


def test_unknown_role_raises_value_error_listing_valid_roles():
    registry, _ = make_registry(make_settings())
    with pytest.raises(ValueError, match="planner, coder, summarizer, verifier"):
        registry.for_role("architect")
    with pytest.raises(ValueError, match="unknown role 'architect'"):
        registry.for_role("architect")


# --- close_all ---------------------------------------------------------------


def test_close_all_closes_every_provider_exactly_once_idempotent():
    registry, created = make_registry(make_settings(planner="big-model", coder="small-model"))
    registry.for_role("planner")
    registry.for_role("coder")
    assert len(created) == 3

    async def go():
        await registry.close_all()
        await registry.close_all()  # second call must be a no-op

    asyncio.run(go())
    assert [p.close_count for p in created] == [1, 1, 1]


def test_closed_registry_refuses_to_build_new_providers():
    registry, created = make_registry(make_settings(planner="big-model"))
    asyncio.run(registry.close_all())
    with pytest.raises(RuntimeError, match="closed"):
        registry.for_role("planner")  # would build a never-closed provider
    assert len(created) == 1


# --- for_model (MS-2 live swaps) ---------------------------------------------


def test_for_model_builds_once_and_caches_per_model():
    registry, created = make_registry(make_settings())
    b1 = registry.for_model("model-b")
    b2 = registry.for_model("model-b")
    assert b1 is b2
    assert b1.model == "model-b"
    assert len(created) == 2  # the default + model-b, one build each


def test_for_model_default_model_reuses_the_default_instance():
    registry, created = make_registry(make_settings())
    assert registry.for_model("default-model") is registry.default
    assert len(created) == 1  # no second build for the default's own model


def test_for_model_shares_the_cache_with_roles():
    registry, created = make_registry(make_settings(planner="model-b"))
    role = registry.for_role("planner")
    assert registry.for_model("model-b") is role  # one client per (endpoint, model)
    assert len(created) == 2


def test_for_model_on_closed_registry_raises():
    registry, _ = make_registry(make_settings())
    asyncio.run(registry.close_all())
    with pytest.raises(RuntimeError, match="closed"):
        registry.for_model("model-b")


# --- plugin provider factories (MS-5) ----------------------------------------


def _plugin_settings(ptype: str) -> Settings:
    return Settings(
        provider=ProviderSettings(
            base_url="http://testserver/v1", api_key="sk-unit-test",
            model="default-model", type=ptype,
        )
    )


def test_select_provider_factory_prefers_a_plugin_type_match():
    from ironcore.providers.registry import select_provider_factory

    def myprov(**kwargs):  # pragma: no cover - selection only, never called
        raise NotImplementedError

    chosen = select_provider_factory(
        _plugin_settings("myprov"), plugin_factories={"myprov": myprov}
    )
    assert chosen is myprov


def test_select_provider_factory_unknown_type_still_falls_through_to_auto():
    from ironcore.providers.registry import select_provider_factory

    # pinned behavior: an unmatched type never breaks boot — auto selection
    # (non-:11434 endpoint -> the generic OpenAI-compatible client)
    chosen = select_provider_factory(_plugin_settings("mystery"), plugin_factories={})
    assert chosen is OpenAICompatProvider


def test_from_settings_provider_factory_passthrough_feeds_the_one_build_path():
    created: list[FakeProvider] = []

    def factory(**kwargs):
        provider = FakeProvider(**kwargs)
        created.append(provider)
        return provider

    settings = Settings(
        provider=ProviderSettings(
            base_url="http://testserver/v1", api_key="sk-unit-test", model="default-model"
        ),
        roles=RoleModels(planner="big-model"),
    )
    registry = ProviderRegistry.from_settings(settings, provider_factory=factory)
    assert registry.default is created[0]
    # for_role and for_model construct through the SAME injected factory
    assert registry.for_role("planner").model == "big-model"
    assert registry.for_model("model-b").model == "model-b"
    assert len(created) == 3
    assert all(isinstance(p, FakeProvider) for p in created)
