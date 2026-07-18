"""CapabilityProfile: what a model can actually do, measured not assumed.

Produced by the probe runner (IC-602..604), cached under
~/.ironcore/envelopes/<slug>.json, and consumed by the adapter ladders
below. All scores are reliabilities in [0, 1] from repeated trials.

The two `recommended_*` ladders are pure functions of the profile and are
frozen behavior (docs/CONTRACTS.md #Envelope): the engine must never pick
a protocol another way.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel, Field

#: Ladder order: most efficient first, most forgiving last.
TOOL_PROTOCOL_LADDER: tuple[str, ...] = ("native", "strict_json", "text_protocol")
TOOL_PROTOCOL_THRESHOLDS: dict[str, float] = {"native": 0.95, "strict_json": 0.90}

EDIT_FORMAT_LADDER: tuple[str, ...] = ("unified_diff", "search_replace", "whole_file")
EDIT_FORMAT_THRESHOLDS: dict[str, float] = {"unified_diff": 0.90, "search_replace": 0.85}


#: a staging file older than this is nobody's live write, so it is safe to
#: reap. Generous on purpose: a concurrent session's in-flight staging file
#: must never be swept out from under it.
_STAGING_STALE_SECONDS = 3600.0


def _sweep_stale_staging(path: Path) -> None:
    """Reap abandoned staging files for ``path``.

    ``_atomic_write_json``'s own ``except OSError`` cleans up a failed write,
    but an interruption that is not an ``OSError`` — a KeyboardInterrupt during
    the first-run probe, which is precisely this package's scenario — unwinds
    past it and strands a ``.<name>.xxxx.tmp``. Each such quit used to litter
    ``~/.ironcore/envelopes/`` permanently. Best-effort: a sweep failure must
    never fail the save it precedes."""
    cutoff = time.time() - _STAGING_STALE_SECONDS
    try:
        stale = list(path.parent.glob(f".{path.name}.*.tmp"))
    except OSError:
        return
    for leftover in stale:
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()
        except OSError:  # locked, or another writer got there first
            continue


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Publish ``payload`` at ``path`` atomically: stage under a UNIQUE name in
    the target's own directory (same volume, so ``os.replace`` is atomic), fsync,
    then rename over the target.

    The staging name is unique per writer, not ``<target>.tmp``: two IronCore
    sessions probing the same model share the envelope dir with no lock, and a
    shared staging name lets writer A publish writer B's half-written bytes. A
    failed write takes its own droppings with it."""
    _sweep_stale_staging(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class CapabilityProfile(BaseModel):
    """Measured capabilities of one model at one endpoint."""

    model_id: str
    probed_at: str | None = None  # ISO-8601; None = defaults, never probed
    #: provenance of these values: "default" (floor), "seeded" (introspected,
    #: provisional), "probed" (measured). Additive field — CONTRACTS.md §5.
    source: str = "default"

    # Context
    context_window: int = 8192  # advertised
    honest_context: int = 4096  # depth at which needle retrieval stays >= 0.9
    #: measured chars per prompt token (TOKEN-RATIO probe); 4.0 = the universal
    #: unmeasured default. The composer's token estimator divides character
    #: counts by this ratio. Additive field — CONTRACTS.md §5; envelope JSONs
    #: written before it exist load as 4.0 via this default.
    chars_per_token: float = 4.0
    #: whether the model accepts image inputs (MS-6). Floor-conservative default
    #: False; seeded from endpoint introspection (Ollama /api/show capabilities),
    #: overridable via [envelope] vision. Consulted ONLY for image attachment —
    #: never for protocol selection. Additive field — CONTRACTS.md §5; envelope
    #: JSONs written before it exist load as False via this default.
    vision: bool = False

    # Reliability scores, [0..1]
    tool_protocols: dict[str, float] = Field(default_factory=dict)
    edit_formats: dict[str, float] = Field(default_factory=dict)
    json_adherence: float = 0.0
    instruction_retention: float = 0.0  # constraint from turn 1 still honored at turn k
    coherence_horizon: int = 6  # turns before drift; drives anchor cadence

    # Sampling defaults discovered to work for this model
    sampling: dict[str, float] = Field(
        default_factory=lambda: {"temperature": 0.2, "top_p": 0.95}
    )

    # ------------------------------------------------------------------ #
    # Adapter ladders (frozen behavior — see module docstring)
    # ------------------------------------------------------------------ #

    def recommended_tool_protocol(self) -> str:
        """First protocol on the ladder whose measured reliability clears its
        threshold; the text protocol is the always-works floor."""
        for proto in TOOL_PROTOCOL_LADDER[:-1]:
            if self.tool_protocols.get(proto, 0.0) >= TOOL_PROTOCOL_THRESHOLDS[proto]:
                return proto
        return TOOL_PROTOCOL_LADDER[-1]

    def recommended_edit_format(self) -> str:
        for fmt in EDIT_FORMAT_LADDER[:-1]:
            if self.edit_formats.get(fmt, 0.0) >= EDIT_FORMAT_THRESHOLDS[fmt]:
                return fmt
        return EDIT_FORMAT_LADDER[-1]

    def anchor_cadence(self) -> int:
        """Re-anchor (re-state goal + constraints) every N turns.
        Weak retention -> anchor more often. Bounded to [2, 12]."""
        return max(2, min(12, self.coherence_horizon))

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    @staticmethod
    def slug(model_id: str) -> str:
        return re.sub(r"[^a-zA-Z0-9._-]+", "_", model_id)

    def save(self, envelope_dir: Path) -> Path:
        """Write the cache ATOMICALLY: stage next to the target (same volume, so
        ``os.replace`` is atomic), fsync, then publish. The first-run probe is
        the one write a stranger's launch performs unasked, and quitting during
        it must never leave a half-written file at the live path — that used to
        brick every later boot AND ``ironcore doctor``."""
        envelope_dir.mkdir(parents=True, exist_ok=True)
        path = envelope_dir / f"{self.slug(self.model_id)}.json"
        _atomic_write_json(path, self.model_dump())
        return path

    @classmethod
    def load(cls, envelope_dir: Path, model_id: str) -> CapabilityProfile | None:
        """The cached profile, or ``None`` when there isn't a usable one.

        Never raises: a missing cache and a CORRUPT cache both read as
        "unprobed" so the model simply re-probes. See ``load_with_note`` for the
        variant that reports corruption to the user."""
        return cls.load_with_note(envelope_dir, model_id)[0]

    @classmethod
    def load_with_note(
        cls, envelope_dir: Path, model_id: str
    ) -> tuple[CapabilityProfile | None, str | None]:
        """``(profile, note)``. ``note`` is non-None only when a corrupt cache
        was quarantined, and names the path so the boot note can say what
        happened. Additive surface — CONTRACTS.md §5."""
        path = Path(envelope_dir) / f"{cls.slug(model_id)}.json"
        try:
            raw = path.read_bytes()
        except OSError:  # missing file = the normal first boot; unreadable = same outcome
            return None, None
        try:
            # Bytes, not text: ``json.loads`` does its own decoding, so a payload
            # that isn't valid UTF-8 (power-loss free-list garbage, an AV-quarantine
            # stub, a cloud-sync conflict copy) raises UnicodeDecodeError — a
            # ValueError — and is quarantined below like any other corrupt cache.
            # Decoding via ``read_text`` instead would raise OUTSIDE this guard and
            # brick every boot, which is the exact failure this class exists to end.
            return cls.model_validate(json.loads(raw)), None
        except ValueError:  # bad bytes, bad JSON (truncated/empty), or bad schema
            pass
        # Quarantine rather than delete: the evidence stays inspectable, and the
        # live path is freed so the next probe can write a good cache.
        quarantine = path.with_name(path.name + ".corrupt")
        try:
            os.replace(path, quarantine)
        except OSError:  # locked/read-only — still boot; the next save overwrites
            return None, f"[envelope] ignored a corrupt cache at {path} (could not quarantine it)"
        return None, (
            f"[envelope] the cached profile for {model_id!r} was corrupt (an interrupted "
            f"write?) — moved to {quarantine} and re-measuring from defaults."
        )
