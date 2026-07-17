"""Probe runner: orchestrate a set of probes into a CapabilityProfile + report card.

IC-601 owns the ORCHESTRATION only. The individual probe implementations land in
IC-602 (CTX-HONESTY, RETENTION), IC-603 (TOOL-FORM, JSON-STRICT), and IC-604
(EDIT-FORMAT, CODE-SMOKE). This module defines the ``Probe`` interface those tasks
implement and the machinery that folds their results into a saved profile + a report
card (rendered by ``/envelope``, IC-608).

Runner rules (SPEC §4.1, MODELS §2):
  * Partial failure never aborts the run. A probe that RAISES or returns ``ok=False``
    degrades its declared *reliability* targets to a conservative ``0.0`` and records a
    note; *context/horizon* targets are left at the base/default (a failed measurement
    must not invent a smaller honest_context). Every other probe still runs.
  * Deterministic + offline. No ``datetime.now``, no network, no disk writes inside
    ``run_probes``: the caller stamps ``probed_at`` and chooses ``envelope_dir``. Probes
    talk to a ``Provider`` (``MockProvider`` in tests), nothing else.
  * The runner never *picks* protocols — it only *fills* fields. Protocol/format
    selection stays in ``CapabilityProfile.recommended_*`` (frozen, CONTRACTS.md §5).

The dotted-path merge convention (CRITICAL — probe authors implement this exactly):
  ``ProbeResult.scores`` maps a *dotted profile path* to the value to merge in place:
    * dict fields   -> ``"tool_protocols.native"``, ``"edit_formats.unified_diff"``,
                       ``"sampling.temperature"``   (value written under that sub-key)
    * scalar fields -> ``"honest_context"``, ``"context_window"``,
                       ``"coherence_horizon"``       (coerced to int)
                       ``"json_adherence"``, ``"instruction_retention"``,
                       ``"chars_per_token"``         (coerced to float)
  A path whose root is ``tool_protocols`` / ``edit_formats`` / ``json_adherence`` /
  ``instruction_retention`` is a RELIABILITY (degrades to ``0.0`` on failure).
  ``honest_context`` / ``context_window`` / ``coherence_horizon`` / ``sampling`` /
  ``chars_per_token`` are NOT reliabilities — a failed probe leaves them at the
  base/default (a failed measurement must not invent a token ratio). Unknown or malformed
  paths from a misbehaving probe are skipped, never fatal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ironcore.envelope.profile import (
    EDIT_FORMAT_LADDER,
    EDIT_FORMAT_THRESHOLDS,
    TOOL_PROTOCOL_LADDER,
    TOOL_PROTOCOL_THRESHOLDS,
    CapabilityProfile,
)
from ironcore.providers.base import Provider

# --------------------------------------------------------------------------- #
# Field taxonomy for the dotted-path merge (see module docstring)
# --------------------------------------------------------------------------- #

_DICT_FIELDS = frozenset({"tool_protocols", "edit_formats", "sampling"})
_INT_FIELDS = frozenset({"honest_context", "context_window", "coherence_horizon"})
#: chars_per_token is deliberately NOT in _RELIABILITY_ROOTS: a failed TOKEN-RATIO
#: measurement keeps the 4.0 base instead of degrading to a nonsense 0.0 ratio.
_FLOAT_FIELDS = frozenset({"json_adherence", "instruction_retention", "chars_per_token"})
#: Roots whose scores are reliabilities in [0, 1] and degrade to 0.0 on failure.
_RELIABILITY_ROOTS = frozenset(
    {"tool_protocols", "edit_formats", "json_adherence", "instruction_retention"}
)


# --------------------------------------------------------------------------- #
# The interface IC-602/603/604 implement
# --------------------------------------------------------------------------- #


@dataclass
class ProbeResult:
    """What one probe reports back to the runner.

    * ``probe_id``   — the probe's stable id (matches ``Probe.id`` / a PROBES entry).
    * ``scores``     — dotted-path -> value, merged into the profile (see module docstring).
    * ``notes``      — human-readable diagnostics for the report / transcript.
    * ``ok``         — False => the probe ran but did not trust its own result; the runner
                       degrades this probe's reliability targets to 0.0.
    """

    probe_id: str
    scores: dict[str, float] = field(default_factory=dict)
    notes: str = ""
    ok: bool = True


class Probe(Protocol):
    """A single capability probe. IC-602/603/604 supply concrete classes.

    Attributes:
      * ``id``      — stable identifier (e.g. ``"TOOL-FORM"``).
      * ``title``   — one-line human description.
      * ``targets`` — the dotted profile paths this probe fills. The runner uses these to
        degrade the right fields when the probe RAISES (no ProbeResult is produced then),
        so declaring them accurately is part of the contract.

    ``run`` performs the trials against ``provider`` and returns a ``ProbeResult``. It may
    raise on catastrophic failure — the runner tolerates that (partial-failure rule).
    """

    id: str
    title: str
    targets: Sequence[str]

    async def run(self, provider: Provider) -> ProbeResult: ...


# --------------------------------------------------------------------------- #
# Merge + degrade helpers
# --------------------------------------------------------------------------- #


def _root(path: str) -> str:
    return path.partition(".")[0]


def _is_reliability_path(path: str) -> bool:
    return _root(path) in _RELIABILITY_ROOTS


def _degraded_scores(targets: Sequence[str]) -> dict[str, float]:
    """Conservative floor for a failed probe: reliability targets -> 0.0; context and
    horizon targets are omitted so they keep the base/default value."""
    return {t: 0.0 for t in targets if _is_reliability_path(t)}


def _merge_score(profile: CapabilityProfile, path: str, value: float) -> None:
    """Write one dotted-path score into ``profile`` in place. Unknown or malformed paths
    are skipped — a probe-author bug must never abort the run."""
    head, _, tail = path.partition(".")
    if head in _DICT_FIELDS and tail:
        getattr(profile, head)[tail] = float(value)
    elif head in _INT_FIELDS and not tail:
        setattr(profile, head, int(value))
    elif head in _FLOAT_FIELDS and not tail:
        setattr(profile, head, float(value))
    # anything else: silently ignored (see module docstring)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


async def evaluate_probes(provider: Provider, probes: Sequence[Probe]) -> list[ProbeResult]:
    """Run every probe against ``provider``, tolerating partial failure.

    A probe that raises or returns ``ok=False`` is converted into a degraded
    ``ProbeResult`` (reliability targets -> 0.0, a note recorded); the run always
    completes. This is the layer that carries per-probe notes — ``run_probes`` folds the
    scores into a profile and drops the notes (the profile has no notes field, so
    ``/envelope`` reads notes from a live run, not the cached profile).
    """
    results: list[ProbeResult] = []
    for probe in probes:
        targets = tuple(getattr(probe, "targets", ()))
        probe_id = getattr(probe, "id", "unknown")
        try:
            result = await probe.run(provider)
        except Exception as exc:  # noqa: BLE001 — partial-failure tolerance is the point
            results.append(
                ProbeResult(
                    probe_id=probe_id,
                    scores=_degraded_scores(targets),
                    notes=(
                        f"probe raised {type(exc).__name__}: {exc}; "
                        "reliabilities degraded to 0.0"
                    ),
                    ok=False,
                )
            )
            continue
        if not result.ok:
            degraded = _degraded_scores(targets or tuple(result.scores))
            base_note = result.notes or "probe reported ok=False"
            results.append(
                ProbeResult(
                    probe_id=result.probe_id,
                    scores=degraded,
                    notes=f"{base_note}; reliabilities degraded to 0.0",
                    ok=False,
                )
            )
            continue
        results.append(result)
    return results


async def run_probes(
    provider: Provider,
    probes: Sequence[Probe],
    *,
    model_id: str,
    base: CapabilityProfile | None = None,
    probed_at: str | None = None,
) -> CapabilityProfile:
    """Run ``probes`` and merge their scores into a fresh CapabilityProfile.

    Starts from ``base`` (deep-copied, never mutated) or floor defaults, stamps
    ``model_id`` + ``probed_at`` (the caller supplies the timestamp — this module is
    deterministic), and folds in every ProbeResult via the dotted-path convention. Does
    NOT write to disk (see ``probe_and_save``) and never aborts on a probe failure (see
    ``evaluate_probes``).
    """
    if base is not None:
        profile = base.model_copy(deep=True)
        profile.model_id = model_id
    else:
        profile = CapabilityProfile(model_id=model_id)
    profile.probed_at = probed_at
    profile.source = "probed"  # a measured profile, whatever base it refined

    for result in await evaluate_probes(provider, probes):
        for path, value in result.scores.items():
            _merge_score(profile, path, value)
    return profile


async def probe_and_save(
    provider: Provider,
    probes: Sequence[Probe],
    *,
    model_id: str,
    envelope_dir: Path,
    probed_at: str | None = None,
    base: CapabilityProfile | None = None,
) -> CapabilityProfile:
    """``run_probes`` then persist via ``CapabilityProfile.save``.

    Returns the saved profile; reload it with
    ``CapabilityProfile.load(envelope_dir, model_id)``.
    """
    profile = await run_probes(
        provider, probes, model_id=model_id, base=base, probed_at=probed_at
    )
    profile.save(envelope_dir)
    return profile


# --------------------------------------------------------------------------- #
# Report card (consumed by /envelope, IC-608)
# --------------------------------------------------------------------------- #


def _rung_line(
    rung: str, scores: dict[str, float], thresholds: dict[str, float], recommended: str
) -> str:
    marker = "->" if rung == recommended else "  "
    if rung in thresholds:
        score = scores.get(rung, 0.0)
        status = "ok" if score >= thresholds[rung] else "below"
        return f"  {marker} {rung:<15} {score:.2f}  (needs >= {thresholds[rung]:.2f}, {status})"
    return f"  {marker} {rung:<15} floor (always works)"


def _is_measured(profile: CapabilityProfile) -> bool:
    """A profile carries real measurements once the deep probe has run: ``source``
    is authoritative, and a stamped ``probed_at`` is honored for hand-built profiles.
    A ``seeded`` profile is provisional (introspected, not measured) even though it,
    too, has ``probed_at is None`` -- ``source`` is what distinguishes the two."""
    return profile.source == "probed" or profile.probed_at is not None


def _source_label(profile: CapabilityProfile) -> str:
    """Honest provenance of the numbers so the user knows guesses from measurements.
    The ``tuned`` branch comes FIRST: a tuned profile is measured-then-adjusted and
    must never be mislabeled plain-measured (or, worse, unprobed)."""
    if profile.source == "tuned":
        return "tuned (measured, then lowered from live-session evidence)"
    if profile.source == "seeded":
        return "seeded (provisional - introspected, measuring in the background)"
    if _is_measured(profile):
        return "measured"
    return "defaults (unprobed)"


def _verdict(profile: CapabilityProfile, proto: str, edit: str) -> str:
    floor = proto == TOOL_PROTOCOL_LADDER[-1] and edit == EDIT_FORMAT_LADDER[-1]
    if profile.source == "tuned":
        # measured base, ladders conservatively lowered by live evidence (MS-8).
        rungs = (
            "floor protocol + whole-file edits" if floor
            else f"{proto} tool calls, {edit} edits"
        )
        return f"tuned - {rungs} (lowered from live-session evidence; run /probe to re-measure)"
    if profile.source == "seeded":
        # provisional-usable: reflects the SEED's ladders, not the floor. A seed with
        # no native signal legitimately lands on the floor rungs -- say so honestly.
        rungs = (
            "floor protocol + whole-file edits" if floor
            else f"{proto} tool calls, {edit} edits"
        )
        return f"seeded (provisional) - {rungs} (introspected, measuring in the background)"
    if _is_measured(profile):
        if floor:
            return "floor-only - text protocol + whole-file edits (usable, slow but safe)"
        return f"usable - {proto} tool calls, {edit} edits"
    return "unprobed - floor-conservative defaults (safe-slow until measured)"


def render_report_card(profile: CapabilityProfile) -> str:
    """Plain-text capability report for ``/envelope`` (IC-608).

    A pure function of the profile: model id, honest vs advertised context, the
    recommended tool protocol + edit format with every rung's reliability, retention /
    coherence, and a one-line verdict. Probe notes are NOT shown here — they live on the
    live run, not the persisted profile.
    """
    proto = profile.recommended_tool_protocol()
    edit = profile.recommended_edit_format()
    if profile.probed_at:
        probed = profile.probed_at
    elif profile.source == "seeded":
        probed = "not yet - seeded from introspection, measuring in the background"
    else:
        probed = "never (floor-conservative defaults)"
    honest = profile.honest_context
    advertised = profile.context_window
    pct = f"{100 * honest / advertised:.0f}%" if advertised else "n/a"

    lines = [
        f"Model:            {profile.model_id}",
        f"Source:           {_source_label(profile)}",
        f"Probed:           {probed}",
        f"Context:          honest {honest:,} / advertised {advertised:,} tokens ({pct})",
        # "(default)" not "(unmeasured)": the existing provenance pin forbids the
        # substring "measured" before the verdict; the Source line carries provenance.
        f"Token ratio:      {profile.chars_per_token:.1f} chars/token"
        + ("" if profile.chars_per_token != 4.0 else "  (default)"),
        "",
        f"Tool protocol:    {proto}  (recommended)",
    ]
    lines += [
        _rung_line(rung, profile.tool_protocols, TOOL_PROTOCOL_THRESHOLDS, proto)
        for rung in TOOL_PROTOCOL_LADDER
    ]
    lines += ["", f"Edit format:      {edit}  (recommended)"]
    lines += [
        _rung_line(rung, profile.edit_formats, EDIT_FORMAT_THRESHOLDS, edit)
        for rung in EDIT_FORMAT_LADDER
    ]
    lines += [
        "",
        f"JSON adherence:   {profile.json_adherence:.2f}",
        f"Retention:        {profile.instruction_retention:.2f}",
        f"Coherence:        {profile.coherence_horizon} turns "
        f"(anchor every {profile.anchor_cadence()})",
        # yes/no only (MS-6): the pre-verdict text must never claim "measured".
        f"Vision:           {'yes' if profile.vision else 'no'}",
        "",
        f"Verdict:          {_verdict(profile, proto, edit)}",
    ]
    return "\n".join(lines)
