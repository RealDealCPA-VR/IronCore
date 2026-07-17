"""RoleRouter (MS-3): per-role (provider, profile) resolution semantics.

Pins the routing contract: unset/identity roles resolve to None (= the caller's
primary pair), routed roles get the registry's cached same-endpoint provider
PLUS that model's own envelope from the on-disk cache (measured entries only —
the same cache-hit rule as /model), floor defaults otherwise, and every failure
mode (closed registry, corrupt cache file) DEGRADES instead of crashing. All
offline: MockProvider / counting factories, tmp_path envelope dirs.
"""

from __future__ import annotations

import asyncio

import pytest

from ironcore.config.settings import ProviderSettings, RoleModels, Settings
from ironcore.core.roles import RoleRouter
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.mock import MockProvider
from ironcore.providers.registry import VALID_ROLES, ProviderRegistry


def _settings(*, model: str = "default-model", **roles) -> Settings:
    return Settings(
        provider=ProviderSettings(
            base_url="http://testserver/v1", api_key="sk-unit-test", model=model
        ),
        roles=RoleModels(**roles),
    )


def _factory(*, base_url: str = "", api_key: str = "", model: str = "") -> MockProvider:
    provider = MockProvider()
    provider.model = model
    return provider


def _measured(model: str, **kw) -> CapabilityProfile:
    return CapabilityProfile(
        model_id=model,
        probed_at="2026-07-16T00:00:00Z",
        source="probed",
        **kw,
    )


# --- unset / identity → None (zero-config) -----------------------------------


def test_unset_roles_resolve_none_for_every_role(tmp_path):
    router = RoleRouter(_settings(), envelope_dir=tmp_path / "env")
    for role in VALID_ROLES:
        assert router.resolve(role) is None


def test_role_equal_to_primary_model_resolves_none(tmp_path):
    settings = _settings(model="big", coder="big")
    registry = ProviderRegistry(settings, provider_factory=_factory)
    router = RoleRouter(settings, registry=registry, envelope_dir=tmp_path / "env")
    assert router.resolve("coder") is None  # identity — the primary pair IS it


def test_routed_role_without_registry_or_seam_degrades_to_none(tmp_path):
    router = RoleRouter(_settings(coder="tiny"), envelope_dir=tmp_path / "env")
    assert router.resolve("coder") is None  # no way to build a provider — degrade


# --- routed role → registry provider + disk profile --------------------------


def test_routed_role_returns_registry_provider_and_measured_disk_profile(tmp_path):
    env = tmp_path / "env"
    _measured(
        "tiny", honest_context=2048, tool_protocols={"native": 1.0}, chars_per_token=3.2
    ).save(env)
    settings = _settings(coder="tiny")
    registry = ProviderRegistry(settings, provider_factory=_factory)
    router = RoleRouter(settings, registry=registry, envelope_dir=env)

    routed = router.resolve("coder")
    assert routed is not None
    provider, profile = routed
    assert provider is registry.for_role("coder")  # the registry's cached instance
    assert provider.model == "tiny"
    # the profile round-tripped from disk — the role runs on ITS envelope
    assert profile.model_id == "tiny"
    assert profile.honest_context == 2048
    assert profile.tool_protocols == {"native": 1.0}
    assert profile.chars_per_token == 3.2
    assert profile.recommended_tool_protocol() == "native"


def test_missing_envelope_file_floors(tmp_path):
    settings = _settings(coder="tiny")
    registry = ProviderRegistry(settings, provider_factory=_factory)
    router = RoleRouter(settings, registry=registry, envelope_dir=tmp_path / "env")
    routed = router.resolve("coder")
    assert routed is not None
    _, profile = routed
    # CONTRACTS §5: unprobed models get floor-conservative defaults
    assert profile.model_id == "tiny"
    assert profile.source == "default" and profile.probed_at is None
    assert profile.recommended_tool_protocol() == "text_protocol"


def test_unmeasured_cache_entry_floors(tmp_path):
    # a SEEDED (provisional, not probed) cache entry is a miss — same rule as /model
    env = tmp_path / "env"
    CapabilityProfile(
        model_id="tiny", source="seeded", honest_context=32768, tool_protocols={"native": 1.0}
    ).save(env)
    settings = _settings(coder="tiny")
    registry = ProviderRegistry(settings, provider_factory=_factory)
    router = RoleRouter(settings, registry=registry, envelope_dir=env)
    _, profile = router.resolve("coder")
    assert profile.source == "default" and profile.honest_context == 4096  # floor


def test_corrupt_cache_entry_floors_never_raises(tmp_path):
    env = tmp_path / "env"
    env.mkdir(parents=True)
    (env / f"{CapabilityProfile.slug('tiny')}.json").write_text("{not json", encoding="utf-8")
    settings = _settings(coder="tiny")
    registry = ProviderRegistry(settings, provider_factory=_factory)
    router = RoleRouter(settings, registry=registry, envelope_dir=env)
    _, profile = router.resolve("coder")  # must not raise
    assert profile.source == "default"


# --- validation + caching -----------------------------------------------------


def test_unknown_role_raises_valueerror_listing_valid_roles(tmp_path):
    router = RoleRouter(_settings(), envelope_dir=tmp_path / "env")
    with pytest.raises(ValueError, match="planner, coder, summarizer, verifier"):
        router.resolve("editor")


def test_profile_cached_per_model_and_shared_across_roles(tmp_path):
    env = tmp_path / "env"
    _measured("tiny", honest_context=2048).save(env)
    settings = _settings(coder="tiny", summarizer="tiny")
    registry = ProviderRegistry(settings, provider_factory=_factory)
    router = RoleRouter(settings, registry=registry, envelope_dir=env)
    _, first = router.resolve("coder")
    _, again = router.resolve("coder")
    _, shared = router.resolve("summarizer")
    assert first is again  # one load, cached
    assert first is shared  # two roles naming one model share the object


def test_set_profile_hot_swaps_the_cached_envelope(tmp_path):
    settings = _settings(coder="tiny")
    registry = ProviderRegistry(settings, provider_factory=_factory)
    router = RoleRouter(settings, registry=registry, envelope_dir=tmp_path / "env")
    _, floor = router.resolve("coder")
    assert floor.probed_at is None
    probed = _measured("tiny", honest_context=2048)
    router.set_profile("tiny", probed)  # a background probe just landed
    _, active = router.resolve("coder")
    assert active is probed


# --- degrade + seams ------------------------------------------------------------


def test_closed_registry_degrades_to_none(tmp_path):
    settings = _settings(coder="tiny")
    registry = ProviderRegistry(settings, provider_factory=_factory)
    asyncio.run(registry.close_all())
    router = RoleRouter(settings, registry=registry, envelope_dir=tmp_path / "env")
    assert router.resolve("coder") is None  # degrade to the primary pair, never crash


def test_providers_seam_wins_over_registry(tmp_path):
    seam = MockProvider()
    settings = _settings(coder="tiny")
    registry = ProviderRegistry(settings, provider_factory=_factory)
    router = RoleRouter(
        settings,
        registry=registry,
        envelope_dir=tmp_path / "env",
        providers={"coder": seam},
        profiles={"tiny": CapabilityProfile(model_id="tiny", honest_context=2048)},
    )
    provider, profile = router.resolve("coder")
    assert provider is seam
    assert profile.honest_context == 2048  # the profiles seam, not a disk load
