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
    from collections.abc import Callable, Mapping

    import httpx

    from ironcore.config.settings import Settings

#: the routable roles, in RoleModels declaration order (SPEC #4.4; the
#: test pins this tuple against config.settings.RoleModels.model_fields)
VALID_ROLES: tuple[str, ...] = ("planner", "coder", "summarizer", "verifier")


def select_provider_factory(
    settings: Settings,
    *,
    plugin_factories: Mapping[str, Callable[..., Provider]] | None = None,
) -> Callable[..., Provider]:
    """Pick the client class from ``settings.provider.type``.

    Local models are overwhelmingly Ollama; ``"auto"`` builds an
    :class:`OllamaProvider` for an Ollama-looking endpoint (which keeps the
    model resident via ``keep_alive`` and exposes ``/api`` introspection —
    a real win for local UX) and the generic OpenAI-compatible client
    otherwise. ``"ollama"``/``"openai"`` force the choice.

    ``plugin_factories`` (additive, MS-5) is consulted FIRST: when
    ``provider.type`` equals an ``ironcore.providers`` entry-point name that
    plugin's factory wins. Built-in behavior is otherwise unchanged — an
    unknown type still falls through to auto (``doctor`` warns, boot never
    breaks).
    """
    from ironcore.providers.ollama import OllamaProvider

    ptype = getattr(settings.provider, "type", "auto")
    if plugin_factories and ptype in plugin_factories:
        return plugin_factories[ptype]
    if ptype == "openai":
        return OpenAICompatProvider
    if ptype == "ollama":
        return OllamaProvider
    return OllamaProvider if ":11434" in settings.provider.base_url else OpenAICompatProvider


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
            provider_factory if provider_factory is not None else select_provider_factory(settings)
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
        provider_factory: Callable[..., Provider] | None = None,
    ) -> ProviderRegistry:
        """The boot path (IC-502): default provider from ``settings.provider``.

        ``provider_factory`` (additive, MS-5) pre-selects the client factory —
        the app passes ``select_provider_factory(settings, plugin_factories=…)``
        so a plugin provider flows through the one ``_build`` path (and thus
        ``for_role``/``for_model`` construct plugin providers too). ``None``
        keeps today's built-in selection."""
        return cls(settings, transport=transport, provider_factory=provider_factory)

    def _ensure_open(self) -> None:
        # handing out an already-closed provider fails far from the cause
        # (httpx's "client has been closed" RuntimeError at request time)
        if self._closed:
            raise RuntimeError("ProviderRegistry is closed; build a new one from settings")

    @property
    def default(self) -> Provider:
        """The provider built from ``settings.provider`` -- every unset role."""
        self._ensure_open()
        return self._default

    def for_role(self, role: str) -> Provider:
        """The provider for one of VALID_ROLES: the default unless
        ``settings.roles.<role>`` names a model, then the cached instance
        for that model at the same endpoint."""
        self._ensure_open()
        if role not in VALID_ROLES:
            valid = ", ".join(VALID_ROLES)
            raise ValueError(f"unknown role {role!r}; valid roles: {valid}")
        model: str | None = getattr(self._settings.roles, role)
        if model is None:
            return self._default
        cached = self._providers.get(model)
        return cached if cached is not None else self._build(model)

    def for_model(self, model: str) -> Provider:
        """The cached-or-new provider for ``model`` at the default endpoint
        (MS-2 live swaps: ``/model <name>`` re-points the running engine).
        Shares the per-model cache the roles use — swapping back to a model
        reuses its instance — and a closed registry raises like ``_build``."""
        self._ensure_open()
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
