"""OutcomeLedger + deterministic ladder tuning (MS-8): the self-improvement loop.

The probe battery measures a model once; live sessions keep producing the same
mechanical evidence forever — did a tool call parse at the active rung, did an
edit apply in the format the model chose, did verification pass, did the turn
drift. This module persists that evidence per model and, at session start,
CONSERVATIVELY folds it back into the capability profile:

* **Downgrade-only.** ``apply_tuning`` may only LOWER a ladder score (or
  ``coherence_horizon``) that live evidence contradicts. It never raises one:
  an upgrade needs a real measurement, so a suspiciously clean live rate emits
  a "run /probe" *hint*, nothing more.
* **The frozen ladders stay the sole selector.** Tuning edits the *scores* the
  frozen ``recommended_*`` functions read (CONTRACTS §5); it never picks a
  protocol itself. A lowered score makes the frozen ladder fall to the next
  rung by itself.
* **Sample semantics.** Every provider CALL iteration is one tool-protocol
  sample at the ACTIVE rung; every real edit APPLY outcome (success or a
  mechanical ``patch_failure``) is one edit-format sample. Min-sample floors
  (``MIN_*_SAMPLES``) provide hysteresis, and counters halve once attempts
  pass ``DECAY_CAP`` so old evidence fades and files stay bounded.
* **Generation-stamped.** ``ensure_stamp`` resets all counters whenever the
  profile generation changes (a fresh probe or seed) — stale evidence must
  never re-downgrade a freshly measured profile. ``generation_stamp`` treats
  a tuned overlay as its measured base, so tuning itself never resets the
  evidence it was computed from.
* **Corruption-tolerant persistence.** The ledger lives in a
  ``<slug>.outcomes.json`` sidecar next to the envelope JSON; a missing or
  corrupt sidecar loads as a fresh ledger — reads never raise. The tuned
  overlay is recomputed at load time and never written back to the envelope
  JSON (the cached profile stays the honest measurement).

Dependency rules: pydantic + stdlib + ``envelope.profile`` only — nothing here
imports core/tools/commands/tui.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, PrivateAttr

from ironcore.envelope.profile import (
    EDIT_FORMAT_LADDER,
    EDIT_FORMAT_THRESHOLDS,
    TOOL_PROTOCOL_LADDER,
    TOOL_PROTOCOL_THRESHOLDS,
    CapabilityProfile,
    _atomic_write_json,
)

#: Counters halve (attempts AND failures) once attempts exceed this, so the
#: ledger is bounded and old evidence decays instead of pinning a model forever.
DECAY_CAP = 200

#: Hysteresis floors: below these sample counts the tuner never acts.
MIN_TOOL_SAMPLES = 10
MIN_EDIT_SAMPLES = 8
MIN_TURN_SAMPLES = 8

#: Drift ratio (drift turns / turns) at which the coherence horizon is lowered.
DRIFT_RATIO = 0.25

#: A live success rate this clean at the recommended rung earns a re-probe HINT
#: for any higher rung the stored profile keeps below threshold (never an
#: automatic upgrade).
REPROBE_RATE = 0.98


def generation_stamp(profile: CapabilityProfile) -> str:
    """Profile-generation stamp for ``OutcomeLedger.ensure_stamp``.

    Changes exactly when the profile's measurement generation changes (a new
    probe timestamp, a seed replacing floor defaults) and is INVARIANT under
    tuning: a ``"tuned"`` overlay carries its measured base's stamp, otherwise
    boot-time tuning would reset the very evidence it was computed from.
    """
    if profile.source in ("probed", "tuned") or profile.probed_at is not None:
        return f"measured:{profile.probed_at or ''}"
    return f"{profile.source}:"


class Counter(BaseModel):
    """Attempt/failure tally for one ladder rung, with halving decay."""

    attempts: int = 0
    failures: int = 0

    def record(self, ok: bool) -> None:
        self.attempts += 1
        if not ok:
            self.failures += 1
        if self.attempts > DECAY_CAP:
            self.attempts //= 2
            self.failures //= 2

    def success_rate(self) -> float:
        """Successes / attempts; NO evidence reads as 1.0 (absence of evidence
        must never look like failure — the tuner also gates on min samples)."""
        if self.attempts <= 0:
            return 1.0
        return 1.0 - (self.failures / self.attempts)


class OutcomeLedger(BaseModel):
    """Per-model live-session evidence, persisted next to the envelope cache.

    Recording methods are cheap and never raise; persistence mirrors
    ``CapabilityProfile.save/load`` (utf-8 JSON, slugged filename) with the
    corruption tolerance of ``memory/sessions.py``: a bad sidecar is a fresh
    ledger, never a crash.
    """

    version: int = 1
    model_id: str
    #: generation stamp of the profile the counters were collected against.
    profile_stamp: str = ""
    tool_protocols: dict[str, Counter] = Field(default_factory=dict)
    edit_formats: dict[str, Counter] = Field(default_factory=dict)
    turns: int = 0
    drift_events: int = 0
    verify_runs: int = 0
    verify_failures: int = 0

    #: where this ledger was loaded from; remembered so ``save()`` and
    #: ``for_model`` (a ``/model`` swap) need no re-resolution. ``None`` for a
    #: directly constructed ledger — ``save()`` is then a no-op.
    _envelope_dir: Path | None = PrivateAttr(default=None)

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #

    def record_tool_attempt(self, protocol: str, ok: bool) -> None:
        self.tool_protocols.setdefault(protocol, Counter()).record(ok)

    def record_edit_attempt(self, fmt: str, ok: bool) -> None:
        self.edit_formats.setdefault(fmt, Counter()).record(ok)

    def record_verify(self, ok: bool) -> None:
        self.verify_runs += 1
        if not ok:
            self.verify_failures += 1
        if self.verify_runs > DECAY_CAP:
            self.verify_runs //= 2
            self.verify_failures //= 2

    def record_turn(self, *, drift: bool) -> None:
        self.turns += 1
        if drift:
            self.drift_events += 1
        if self.turns > DECAY_CAP:
            self.turns //= 2
            self.drift_events //= 2

    def ensure_stamp(self, stamp: str) -> bool:
        """Reset every counter when the profile generation changed.

        Called at turn start with ``generation_stamp(active_profile)``: evidence
        collected against an old generation (pre-probe floor, a prior probe) must
        not re-downgrade a freshly measured profile. Returns True on a reset.
        """
        if stamp == self.profile_stamp:
            return False
        self.profile_stamp = stamp
        self.tool_protocols.clear()
        self.edit_formats.clear()
        self.turns = 0
        self.drift_events = 0
        self.verify_runs = 0
        self.verify_failures = 0
        return True

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    @staticmethod
    def path_for(envelope_dir: Path, model_id: str) -> Path:
        return Path(envelope_dir) / f"{CapabilityProfile.slug(model_id)}.outcomes.json"

    def save(self, envelope_dir: Path | None = None) -> Path | None:
        """Write the sidecar; ``None`` = the remembered load dir. May raise
        ``OSError`` (callers treat persistence as best-effort, like state.save);
        with no directory known at all this is a silent no-op."""
        target = Path(envelope_dir) if envelope_dir is not None else self._envelope_dir
        if target is None:
            return None
        self._envelope_dir = target
        target.mkdir(parents=True, exist_ok=True)
        path = self.path_for(target, self.model_id)
        # Atomic like every other persistence path in this codebase: stage on the
        # same volume, fsync, publish. A crash mid-write leaves the PREVIOUS
        # ledger intact instead of a truncated one.
        _atomic_write_json(path, self.model_dump())
        return path

    @classmethod
    def load(cls, envelope_dir: Path, model_id: str) -> OutcomeLedger:
        """Load ``model_id``'s ledger; missing/corrupt/mismatched → a FRESH
        ledger. Never raises — evidence is an optimization, not a dependency."""
        ledger: OutcomeLedger | None = None
        try:
            raw = cls.path_for(envelope_dir, model_id).read_text(encoding="utf-8")
            ledger = cls.model_validate(json.loads(raw))
        except (OSError, ValueError):  # missing file, bad JSON, bad schema
            ledger = None
        if ledger is None or ledger.model_id != model_id:
            ledger = cls(model_id=model_id)
        ledger._envelope_dir = Path(envelope_dir)
        return ledger

    def for_model(self, model_id: str) -> OutcomeLedger:
        """The ledger for ``model_id``: self when it already matches, else a
        load-or-create from the same envelope dir (the ``/model`` repoint path).
        With no remembered dir the new ledger is in-memory only."""
        if model_id == self.model_id:
            return self
        if self._envelope_dir is None:
            return OutcomeLedger(model_id=model_id)
        return OutcomeLedger.load(self._envelope_dir, model_id)


# --------------------------------------------------------------------------- #
# The deterministic tuner
# --------------------------------------------------------------------------- #


@dataclass
class TuningResult:
    """What ``apply_tuning`` decided: the (possibly adjusted) profile copy,
    human-readable adjustment notes, and upgrade hints (never applied)."""

    profile: CapabilityProfile
    adjustments: list[str] = field(default_factory=list)
    reprobe_hints: list[str] = field(default_factory=list)


def _tune_ladder(
    scores: dict[str, float],
    counters: dict[str, Counter],
    ladder: tuple[str, ...],
    thresholds: dict[str, float],
    label: str,
    min_samples: int,
    adjustments: list[str],
) -> None:
    """Lower any above-threshold stored score whose live rate falls below the
    FROZEN threshold — the frozen ``recommended_*`` ladder then falls to the
    next rung on its own. ``min(stored, live)`` can only ever lower."""
    for rung in ladder[:-1]:
        counter = counters.get(rung)
        if counter is None or counter.attempts < min_samples:
            continue
        live = counter.success_rate()
        threshold = thresholds[rung]
        stored = scores.get(rung, 0.0)
        if live < threshold and stored >= threshold:
            scores[rung] = min(stored, live)
            adjustments.append(
                f"{label} {rung}: stored {stored:.2f} but live success {live:.2f} "
                f"over {counter.attempts} attempts (needs >= {threshold:.2f}) - "
                f"lowered to {scores[rung]:.2f}"
            )


def _ladder_hints(
    scores: dict[str, float],
    counters: dict[str, Counter],
    ladder: tuple[str, ...],
    thresholds: dict[str, float],
    recommended: str,
    label: str,
    min_samples: int,
) -> list[str]:
    """Upgrade HINTS only: a clean live rate at the recommended rung, with a
    higher rung stored below threshold, asks for a re-measure — it never edits
    the profile (upgrades require a real probe)."""
    counter = counters.get(recommended)
    if counter is None or counter.attempts < min_samples:
        return []
    live = counter.success_rate()
    if live < REPROBE_RATE:
        return []
    hints: list[str] = []
    for rung in ladder[: ladder.index(recommended)]:
        if scores.get(rung, 0.0) < thresholds[rung]:
            hints.append(
                f"{label} {recommended} is succeeding ({live:.2f} over "
                f"{counter.attempts} attempts) - run /probe to re-measure {rung} "
                "(upgrades are never applied automatically)"
            )
    return hints


def apply_tuning(
    profile: CapabilityProfile,
    ledger: OutcomeLedger,
    *,
    min_tool_samples: int = MIN_TOOL_SAMPLES,
    min_edit_samples: int = MIN_EDIT_SAMPLES,
    min_turn_samples: int = MIN_TURN_SAMPLES,
) -> TuningResult:
    """Fold live evidence into a DEEP COPY of ``profile`` (input never mutated).

    Applies only when the evidence is trustworthy: the profile is MEASURED
    (tuning floor defaults is meaningless — they are already the bottom), the
    ledger belongs to this model, and its generation stamp matches (evidence
    against another generation is void, ``ensure_stamp`` will reset it). If
    anything was adjusted the copy is marked ``source="tuned"`` (CONTRACTS §5)
    with ``probed_at`` preserved.
    """
    tuned = profile.model_copy(deep=True)
    measured = profile.source in ("probed", "tuned") or profile.probed_at is not None
    if (
        not measured
        or ledger.model_id != profile.model_id
        or ledger.profile_stamp != generation_stamp(profile)
    ):
        return TuningResult(profile=tuned)

    adjustments: list[str] = []
    _tune_ladder(
        tuned.tool_protocols,
        ledger.tool_protocols,
        TOOL_PROTOCOL_LADDER,
        TOOL_PROTOCOL_THRESHOLDS,
        "tool protocol",
        min_tool_samples,
        adjustments,
    )
    _tune_ladder(
        tuned.edit_formats,
        ledger.edit_formats,
        EDIT_FORMAT_LADDER,
        EDIT_FORMAT_THRESHOLDS,
        "edit format",
        min_edit_samples,
        adjustments,
    )
    if ledger.turns >= min_turn_samples and ledger.drift_events / ledger.turns >= DRIFT_RATIO:
        horizon = tuned.coherence_horizon
        lowered = max(2, horizon - 2)
        if lowered < horizon:
            tuned.coherence_horizon = lowered
            adjustments.append(
                f"coherence horizon: drift in {ledger.drift_events}/{ledger.turns} "
                f"turns (ratio >= {DRIFT_RATIO:.2f}) - lowered {horizon} -> {lowered} "
                "(anchoring more often)"
            )

    hints = _ladder_hints(
        tuned.tool_protocols,
        ledger.tool_protocols,
        TOOL_PROTOCOL_LADDER,
        TOOL_PROTOCOL_THRESHOLDS,
        tuned.recommended_tool_protocol(),
        "tool protocol",
        min_tool_samples,
    )
    hints += _ladder_hints(
        tuned.edit_formats,
        ledger.edit_formats,
        EDIT_FORMAT_LADDER,
        EDIT_FORMAT_THRESHOLDS,
        tuned.recommended_edit_format(),
        "edit format",
        min_edit_samples,
    )

    if adjustments:
        tuned.source = "tuned"  # probed_at preserved: the base measurement stands
    return TuningResult(profile=tuned, adjustments=adjustments, reprobe_hints=hints)
