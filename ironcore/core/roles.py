"""RoleRouter (MS-3): a model per role, each with its own measured envelope.

The ``[roles]`` config (SPEC §4.4, ``config.settings.RoleModels``) has always
been able to NAME a different model per role, and ``ProviderRegistry.for_role``
has always been able to BUILD a cached same-endpoint provider for it — but the
engine sent every call to its single primary ``(provider, profile)`` pair. This
module closes that gap: :class:`RoleRouter` resolves a role to the routed
provider **plus that model's own capability envelope**, read from the same
on-disk cache ``/probe`` and ``/model`` (MS-2) write to, so a routed planner /
coder / summarizer runs on ITS measured wire protocol, context window, and
sampling — not the primary model's.

Resolution semantics (pinned by ``tests/test_roles.py``):

* ``resolve(role)`` returns ``None`` — meaning "use the caller's primary pair"
  — when the role is unset, when the role model equals ``provider.model``
  (identity, mirroring the registry), or when no provider can be produced
  (closed/absent registry): routing DEGRADES, it never crashes a turn.
* Profiles come from the per-model cache: an injected seam, then the envelope
  dir (the SAME directory MS-2 resolved — callers pass it in; never re-derive
  it here), accepting only MEASURED profiles (``source == "probed"`` or a
  ``probed_at`` stamp — the same cache-hit rule as ``/model``'s swap), else
  the floor-conservative default (CONTRACTS §5: unprobed models get floor
  defaults). Two roles naming one model share one profile object.
* ``set_profile(model_id, profile)`` hot-swaps a role model's envelope (a
  background probe landing mid-session), mirroring ``engine.profile`` swaps.

Dependency rules: core may import providers/envelope/config; this module is
imported by ``core.engine`` and must never import tools/commands/tui.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.registry import VALID_ROLES

if TYPE_CHECKING:  # annotations only — keep engine imports light
    from pathlib import Path

    from ironcore.config.settings import Settings
    from ironcore.providers.base import Provider
    from ironcore.providers.registry import ProviderRegistry


class RoleRouter:
    """Resolve a role name to its routed ``(provider, profile)`` pair.

    ``providers`` (keyed by ROLE) and ``profiles`` (keyed by MODEL id) are test
    seams that win over the registry / envelope cache; ``profiles`` doubles as
    the per-model cache, so resolved profiles are shared and hot-swappable.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        registry: ProviderRegistry | None = None,
        envelope_dir: Path | None = None,
        providers: dict[str, Provider] | None = None,
        profiles: dict[str, CapabilityProfile] | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._envelope_dir = envelope_dir
        self._providers: dict[str, Provider] = dict(providers or {})
        #: model id -> profile: the seam AND the cache (one object per model).
        self._profiles: dict[str, CapabilityProfile] = dict(profiles or {})

    def resolve(self, role: str) -> tuple[Provider, CapabilityProfile] | None:
        """The routed pair for ``role``, or ``None`` = use the primary pair."""
        if role not in VALID_ROLES:
            valid = ", ".join(VALID_ROLES)  # ValueError parity with the registry
            raise ValueError(f"unknown role {role!r}; valid roles: {valid}")
        model: str | None = getattr(self._settings.roles, role)
        if model is None or model == self._settings.provider.model:
            return None  # unset, or identity — the primary pair already IS it
        provider = self._providers.get(role)
        if provider is None:
            if self._registry is None:
                return None
            try:
                provider = self._registry.for_role(role)
            except Exception:  # noqa: BLE001 — closed registry: degrade, never crash a turn
                return None
        return provider, self._profile_for(model)

    def set_profile(self, model_id: str, profile: CapabilityProfile) -> None:
        """Hot-swap the cached envelope for ``model_id`` (a probe just landed)."""
        self._profiles[model_id] = profile

    def _profile_for(self, model: str) -> CapabilityProfile:
        cached = self._profiles.get(model)
        if cached is None:
            cached = self._load_measured(model) or CapabilityProfile(model_id=model)
            self._profiles[model] = cached
        return cached

    def _load_measured(self, model: str) -> CapabilityProfile | None:
        """The on-disk profile iff MEASURED (the MS-2 ``/model`` cache-hit rule);
        a missing, unmeasured, or corrupt entry reads as a miss — floor defaults."""
        envelope_dir = self._envelope_dir
        if envelope_dir is None:
            # lazy import: tests monkeypatch ironcore.envelope.suite.default_envelope_dir
            from ironcore.envelope.suite import default_envelope_dir

            envelope_dir = default_envelope_dir()
        try:
            profile = CapabilityProfile.load(envelope_dir, model)
        except Exception:  # noqa: BLE001 — an unreadable cache file is a miss, not a crash
            return None
        if profile is not None and (profile.source == "probed" or profile.probed_at is not None):
            return profile
        return None
