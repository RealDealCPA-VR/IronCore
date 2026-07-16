"""The default probe battery + the one-call runtime entry point.

``probe_model`` is what makes IronCore actually MOLD to the model it is
pointed at: it runs the seven-probe suite (docs/MODELS.md §2) against a live
provider, caches the resulting :class:`CapabilityProfile` under the envelope
dir, and returns it. The engine then hot-swaps that profile and adapts its
wire protocol, edit format, context budget, token-ratio estimate, anchor
cadence, and sampling.

Both the ``/probe`` command and the app's first-use auto-probe call
``probe_model`` — one measurement path, one cache, one source of truth.
"""

from __future__ import annotations

from pathlib import Path

from ironcore.envelope.probe_ctx import CtxHonestyProbe, RetentionProbe
from ironcore.envelope.probe_edits import CodeSmokeProbe, EditFormatProbe
from ironcore.envelope.probe_ratio import TokenRatioProbe
from ironcore.envelope.probe_tools import JsonStrictProbe, ToolFormProbe
from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.runner import Probe, probe_and_save
from ironcore.providers.base import Provider


def default_envelope_dir() -> Path:
    """Where measured profiles are cached: ``~/.ironcore/envelopes``."""
    return Path.home() / ".ironcore" / "envelopes"


def default_probe_suite() -> list[Probe]:
    """The seven-probe battery: context honesty, retention, tool-form, JSON
    adherence, edit-format, the code-smoke floor gate, and the token-ratio
    measurement. Order is cosmetic; each probe declares the profile fields it
    fills."""
    return [
        CtxHonestyProbe(),
        RetentionProbe(),
        ToolFormProbe(),
        JsonStrictProbe(),
        EditFormatProbe(),
        CodeSmokeProbe(),
        TokenRatioProbe(),
    ]


async def probe_model(
    provider: Provider,
    *,
    model_id: str,
    envelope_dir: Path | None = None,
    probed_at: str,
    probes: list[Probe] | None = None,
    base: CapabilityProfile | None = None,
) -> CapabilityProfile:
    """Measure ``model_id`` on ``provider`` and cache its profile.

    The caller stamps ``probed_at`` (this module is deterministic). Never
    aborts on a single probe failure — a failed measurement degrades that
    field to its conservative floor (see ``run_probes``). ``base`` (e.g. an
    instant-on seed) is refined, not replaced: fields no probe measures keep
    their base value instead of collapsing to the floor default.
    """
    return await probe_and_save(
        provider,
        probes if probes is not None else default_probe_suite(),
        model_id=model_id,
        envelope_dir=envelope_dir if envelope_dir is not None else default_envelope_dir(),
        probed_at=probed_at,
        base=base,
    )
