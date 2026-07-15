"""Provider registry + role routing (IC-204).

SPEC #4.4: the ``[roles]`` config maps planner/coder/summarizer/verifier to
different models. The router is config-driven -- no automatic model selection
in v0.x (predictability beats cleverness). v0.x scope is ONE endpoint, MANY
models: a role model is a model NAME served at the same base_url, so a routed
role gets its own provider instance sharing the default's base_url and
api_key but with ``model=<role model>``. An unset role routes to the default
provider itself (the same object, never a copy).

Behavior (pinned by tests/providers/test_registry.py):

* Providers are cached per model name: two roles routed to the same model
  share one instance, and a role routed to the default's own model shares
  the default instance -- one client per (endpoint, model).
* ``transport=`` is the test seam (HANDOFF agent-ic201-202): forwarded to
  every constructed provider, and omitted entirely when None so substitute
  factories need not accept it.
* ``provider_factory`` is injectable so tests can count instantiations; it
  is called as ``factory(base_url=..., api_key=..., model=...[, transport=...])``
  and defaults to OpenAICompatProvider.
* ``close_all()`` closes every constructed provider exactly once and is
  safe to call twice; a closed registry refuses to build new providers
  (they would leak their transport).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ironcore.providers.base import Provider
from ironcore.providers.openai_compat import OpenAICompatProvider

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from ironcore.config.settings import Settings

#: the routable roles, in RoleModels declaration order (SPEC #4.4; the
#: test pins this tuple against config.settings.RoleModels.model_fields)
VALID_ROLES: tuple[str, ...] = ("planner", "coder", "summarizer", "verifier")


class ProviderRegistry:
    """See module docstring."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider_factory: Callable[..., Provider] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._factory: Callable[..., Provider] = (
            provider_factory if provider_factory is not None else OpenAICompatProvider
        )
        self._transport = transport
        self._closed = False
        #: model name -> constructed provider (seeded by the default build,
        #: so a role routed to the default model reuses the default instance)
        self._providers: dict[str, Provider] = {}
        self._default = self._build(settings.provider.model)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> ProviderRegistry:
        """The boot path (IC-502): default provider from ``settings.provider``."""
        return cls(settings, transport=transport)

    @property
    def default(self) -> Provider:
        """The provider built from ``settings.provider`` -- every unset role."""
        return self._default

    def for_role(self, role: str) -> Provider:
        """The provider for one of VALID_ROLES: the default unless
        ``settings.roles.<role>`` names a model, then the cached instance
        for that model at the same endpoint."""
        if role not in VALID_ROLES:
            valid = ", ".join(VALID_ROLES)
            raise ValueError(f"unknown role {role!r}; valid roles: {valid}")
        model: str | None = getattr(self._settings.roles, role)
        if model is None:
            return self._default
        cached = self._providers.get(model)
        return cached if cached is not None else self._build(model)

    def _build(self, model: str) -> Provider:
        if self._closed:
            # a provider built now could never be closed by close_all()
            raise RuntimeError("ProviderRegistry is closed; cannot build new providers")
        kwargs: dict[str, Any] = {}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        provider = self._factory(
            base_url=self._settings.provider.base_url,
            api_key=self._settings.provider.api_key,
            model=model,
            **kwargs,
        )
        self._providers[model] = provider
        return provider

    async def close_all(self) -> None:
        """Close every constructed provider exactly once; safe to call twice."""
        if self._closed:
            return
        self._closed = True
        for provider in self._providers.values():
            await provider.close()
