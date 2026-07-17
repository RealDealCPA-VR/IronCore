"""/model (IC-801 + MS-2): list the endpoint's models, or LIVE-SWITCH the active model.

    /model            — list the models the endpoint serves, marking the current
                        one and which have a measured envelope cached
    /model <name>     — switch to ``<name>``: settings AND the live session

Switching (MS-2) re-points the RUNNING engine when a live session is attached:
``ProviderRegistry.for_model`` hands out a cached-or-new provider for the name
(one client per (endpoint, model) — swap-back reuses the instance), and the
on-disk envelope cache decides the profile:

* cache HIT — a MEASURED profile exists on disk: hot-swap it instantly; the
  very next turn uses the measured wire protocol / edit format / context budget.
* cache MISS — the engine runs on floor defaults for the new model immediately
  while a scheduled background task seeds the profile from endpoint
  introspection (~1s) then deep-probes + caches it (``probe_and_swap``) — the
  same instant-on molding the app does at first launch.

A running turn refuses the swap (press Esc first). Headless callers (no
``engine``/``provider_registry`` in ``ctx.extra``) and a closed registry fall
back to the IC-801 behavior: update settings and advise a re-probe. Listing
calls ``Provider.list_models`` (async) via ``schedule``. Every ``ctx.extra``
key is optional.
"""

from __future__ import annotations

from pathlib import Path

from ironcore.commands.base import CommandContext, SlashCommand
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.openai_compat import ProviderError


def _cmd_model(ctx: CommandContext, args: str) -> str:
    args = args.strip()
    settings = ctx.settings
    current = settings.provider.model

    if args:
        return _switch_model(ctx, args)

    registry = ctx.extra.get("provider_registry")
    schedule = ctx.extra.get("schedule")
    if registry is None or schedule is None:
        return f"Current model: {current}\n(Listing models needs a live endpoint.)"

    provider = _default_provider(registry)
    if provider is None:
        return f"Current model: {current}\n(The endpoint connection is closed; cannot list.)"
    schedule(_list_models(provider, current, envelope_dir=ctx.extra.get("envelope_dir")))
    return f"Listing models at the endpoint… (current: {current})"


def _switch_model(ctx: CommandContext, name: str) -> str:
    """The MS-2 live swap; falls back to the IC-801 settings-only switch."""
    settings = ctx.settings
    current = settings.provider.model

    # Busy guard: the engine reads self.provider each loop iteration, so a swap
    # mid-turn would tear the running turn. Duck-typed — commands never import tui.
    app = ctx.extra.get("app")
    running = getattr(app, "_turn_running", None)
    if callable(running) and running():
        return "A turn is running — press Esc to interrupt it, then /model again."

    if name == current:
        return f"Already on {name!r} — nothing to switch."

    settings.provider.model = name

    engine = ctx.extra.get("engine")
    registry = ctx.extra.get("provider_registry")
    provider = None
    if engine is not None and registry is not None:
        try:
            provider = registry.for_model(name)
        except Exception:  # noqa: BLE001 — closed registry → settings-only fallback
            provider = None
    if provider is None:
        # Headless / no live registry: the IC-801 behavior (wording pinned by tests).
        return (
            f"Model switched to {name!r} (was {current!r}). The configured model is updated; "
            "the live endpoint connection re-points on the next session — run /probe to "
            "re-profile capabilities for the new model."
        )

    envelope_dir = ctx.extra.get("envelope_dir")
    if envelope_dir is None:
        # lazy import: tests monkeypatch ironcore.envelope.suite.default_envelope_dir
        from ironcore.envelope.suite import default_envelope_dir

        envelope_dir = default_envelope_dir()

    cached = _load_measured(envelope_dir, name)
    if cached is not None:
        # cache HIT: instant hot-swap to the measured profile, no background work.
        engine.repoint(provider, cached)
        return (
            f"Switched to {name!r} (was {current!r}) — envelope loaded from cache "
            f"(measured; tools={cached.recommended_tool_protocol()}, "
            f"edits={cached.recommended_edit_format()}, ctx={cached.honest_context})."
        )

    # cache MISS: usable immediately on floor defaults, measured in the background.
    engine.repoint(provider, CapabilityProfile(model_id=name))
    schedule = ctx.extra.get("schedule")
    if schedule is None:
        return (
            f"Switched to {name!r} (was {current!r}) — no cached envelope; running on floor "
            "defaults (run /probe to measure + adapt to it)."
        )
    schedule(_seed_then_deepen(engine, name, schedule, envelope_dir))
    return (
        f"Switched to {name!r} (was {current!r}) — no cached envelope; measuring it in the "
        "background (floor defaults now; the profile hot-swaps as measurements land)."
    )


async def _seed_then_deepen(engine, name: str, schedule, envelope_dir: Path) -> str:
    """Background molding after a cache-miss swap (mirrors app ``_mold_to_model``):
    SEED from endpoint introspection (~1s, hot-swap #1) when the provider is
    endpoint-backed, then schedule the deep probe (``probe_and_swap``, hot-swap
    #2) as its OWN task so this seed note posts promptly while the ~1-2 min
    deepen runs. Never raises past its guard."""
    from ironcore.commands.envelopecmd import probe_and_swap

    note = ""
    if getattr(engine.provider, "base_url", None) is not None:
        try:
            from ironcore.envelope.seed import seed_profile

            seed = await seed_profile(engine.provider, model_id=name)
            engine.profile = seed  # provisional — the deep probe refines it
            note = (
                f"Seeded {name!r} from the endpoint: context {seed.honest_context}, "
                f"tools {seed.recommended_tool_protocol()!r}, "
                f"edits {seed.recommended_edit_format()!r} "
                "(provisional — measuring in the background)."
            )
        except Exception as exc:  # noqa: BLE001 — seeding must never kill the swap
            note = f"[seed skipped for {name!r}] {exc}"
    schedule(probe_and_swap(engine, envelope_dir=envelope_dir))
    return note


def _load_measured(envelope_dir: Path, model: str) -> CapabilityProfile | None:
    """The cached profile for ``model`` iff it is MEASURED (probed). A missing,
    unmeasured, or corrupt cache entry reads as a miss — never breaks the swap."""
    try:
        profile = CapabilityProfile.load(Path(envelope_dir), model)
    except Exception:  # noqa: BLE001 — an unreadable cache file is a miss, not a crash
        return None
    if profile is not None and (profile.source == "probed" or profile.probed_at is not None):
        return profile
    return None


def _default_provider(registry):
    try:
        return registry.default
    except Exception:  # noqa: BLE001 — closed registry → no provider to list with
        return None


async def _list_models(provider, current: str, *, envelope_dir: Path | None = None) -> str:
    try:
        models = await provider.list_models()
    except ProviderError as exc:
        return f"Could not list models: {exc}"
    if not models:
        return "The endpoint reported no models."
    lines = ["Models at the endpoint:"]
    for model in models:
        # suffix-only additions: "(current)" wording is pinned by IC-801 tests
        line = f"  * {model}  (current)" if model == current else f"    {model}"
        if envelope_dir is not None and _load_measured(envelope_dir, model) is not None:
            line += "  · measured"
        lines.append(line)
    if current not in models:
        lines.append(
            f"\nConfigured model {current!r} is not in the list (switch with /model <name>)."
        )
    return "\n".join(lines)


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        "model", "switch the live model / list models at the endpoint", "/model [name]",
        _cmd_model,
    ),
)
