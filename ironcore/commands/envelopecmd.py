"""/envelope + /probe (IC-608): see and (re)measure the model's capabilities.

    /envelope   — show the current model's capability profile (report card)
    /probe      — measure the live model (context / tool-calling / edit formats)
                  and cache + hot-swap the profile so the engine adapts to it

``/probe`` is the switch that makes IronCore mold to the model: it runs the
probe battery against the live provider (~1-2 min of model calls), caches the
result under ``~/.ironcore/envelopes``, and hot-swaps ``engine.profile`` so the
very next turn uses the measured wire protocol, edit format, context budget,
anchor cadence, and sampling. It is async (via the scheduler); ``/envelope`` is
a synchronous read of the current profile. Every ``ctx.extra`` key is optional.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ironcore.commands.base import CommandContext, SlashCommand
from ironcore.envelope.runner import render_report_card


def _cmd_envelope(ctx: CommandContext, args: str) -> str:
    engine = ctx.extra.get("engine")
    if engine is None:
        return "No live session — /envelope shows the current model's measured profile."
    profile = engine.profile
    card = render_report_card(profile)
    if profile.probed_at is None:
        card += "\n\nThis model is UNPROBED (floor defaults). Run /probe to measure + adapt to it."
    return card


def _cmd_probe(ctx: CommandContext, args: str) -> str:
    engine = ctx.extra.get("engine")
    schedule = ctx.extra.get("schedule")
    if engine is None:
        return "Probing needs a live session (no engine available)."
    if schedule is None:
        return "Probing needs the scheduler (not available here)."
    model = _model_id(engine)
    schedule(probe_and_swap(engine))
    return (
        f"Probing {model!r}… measuring context depth, tool-calling, and edit formats "
        "(~1-2 min of model calls). The profile hot-swaps when it finishes."
    )


def _model_id(engine: Any) -> str:
    return getattr(engine.profile, "model_id", "") or engine.settings.provider.model


async def probe_and_swap(engine: Any) -> str:
    """Measure the live model, cache it, hot-swap ``engine.profile``, and return
    the new report card. Any failure is caught so the UI never crashes."""
    from ironcore.envelope.suite import probe_model

    model = _model_id(engine)
    try:
        # base=current profile so the deep probe REFINES it: an introspected
        # honest_context (from an instant-on seed) survives a probe failure
        # instead of collapsing back to the 4096 floor.
        profile = await probe_model(
            engine.provider,
            model_id=model,
            probed_at=datetime.now(UTC).isoformat(),
            base=engine.profile,
        )
    except Exception as exc:  # noqa: BLE001 — a probe failure must not kill the UI
        return f"[probe failed for {model!r}] {exc}"
    engine.profile = profile  # hot-swap: the next turn adapts to what was measured
    return f"Probe complete — {model!r} profile updated:\n\n{render_report_card(profile)}"


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        "envelope", "show the current model's capability profile", "/envelope", _cmd_envelope
    ),
    SlashCommand(
        "probe", "measure the live model + adapt to it", "/probe", _cmd_probe
    ),
)
