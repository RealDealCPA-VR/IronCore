"""Instant-on seed: a usable CapabilityProfile in ~1s from endpoint introspection.

Boot cost today is the cold probe: an unprobed model runs on the floor default
(text protocol, whole-file edits, 4096 honest context) while a ~2-minute suite
measures it in the background. Yet two cheap signals already exist at boot —
Ollama's real context window (``/api/show``) and endpoint capability detection
(:func:`ironcore.providers.detect.detect`) — so ``seed_profile`` assembles a
*provisional but usable* profile from them in ~1-2s, to be refined (not replaced)
by the deep probe passed as its ``base`` (see ``run_probes(base=...)``).

The seed is deliberately optimistic where the endpoint gives a signal: the deep
probe corrects it within minutes, and the engine's repair loop + downgrade ladders
absorb an over-optimistic seed (a native call that fails → repair → ladder down for
that turn). A conservative seed would leave the user waiting on the floor for no
reason. It is never cached — only the measured profile is saved (``probed_at=None``
here keeps the model "unprobed", so the deep probe still runs).

Resilience: every introspection call is best-effort. ``show_model`` failure keeps
the context default; ``detect`` failure keeps the floor tool/edit ladders.
``seed_profile`` NEVER raises. The api_key never leaks — ``detect`` guarantees it,
and nothing here logs, raises, or stores anything derived from the key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.detect import detect

if TYPE_CHECKING:
    from ironcore.providers.base import Provider

#: cap an unmeasured advertised window: never seed an honest_context beyond what
#: the server will actually process without a measurement (MODELS.md §7 trap).
_UNMEASURED_HONEST_CAP = 32768


async def seed_profile(
    provider: Provider,
    *,
    model_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CapabilityProfile:
    """Introspect ``provider`` into a provisional, usable ``CapabilityProfile``.

    * capabilities — :func:`detect` (never raises): native tools detected →
      ``tool_protocols={"native": 0.95}`` (clears the ladder threshold, a *usable*
      seed, beyond ``as_priors``) and ``edit_formats={"search_replace": 0.85}`` (a
      safe middle rung); otherwise the text/whole-file floors (``{}``).
    * context — Ollama ``show_model`` only, best-effort: ``context_window`` = the
      advertised window; ``honest_context`` = the server's configured ``num_ctx``
      (the real ceiling) when pinned, else the advertised window capped at
      ``_UNMEASURED_HONEST_CAP``. Any failure keeps the floor defaults.

    ``source="seeded"``, ``probed_at=None`` (still unprobed → the deep probe runs).
    Not saved to disk — the seed is provisional. Never raises; completes fast.
    """
    profile = CapabilityProfile(model_id=model_id, probed_at=None, source="seeded")

    # Capabilities: detect() is contractually non-raising and never leaks the key.
    features = await detect(
        provider.base_url,
        getattr(provider, "api_key", ""),
        model=model_id,
        transport=transport,
    )

    # Context: Ollama /api/show, best-effort. num_ctx is the server's REAL ceiling
    # (the truncation trap) — never seed beyond what the server will process.
    show = getattr(provider, "show_model", None)
    if callable(show):
        try:
            details = await show(model_id)
            window = details.context_length or 8192
            profile.context_window = window
            if details.num_ctx_configured:
                # the server's pinned ceiling, but never above the model's own
                # window (a num_ctx pinned higher can't buy real retrieval depth)
                profile.honest_context = min(details.num_ctx_configured, window)
            else:
                profile.honest_context = min(window, _UNMEASURED_HONEST_CAP)
        except Exception:  # noqa: BLE001 — best-effort introspection; keep the defaults
            pass

    # Ladders: a usable seed where the endpoint accepts native tool-calling.
    if features.native_tools:
        profile.tool_protocols = {"native": 0.95}
        profile.edit_formats = {"search_replace": 0.85}

    return profile
