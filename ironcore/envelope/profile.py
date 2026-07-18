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
from pathlib import Path

from pydantic import BaseModel, Field

#: Ladder order: most efficient first, most forgiving last.
TOOL_PROTOCOL_LADDER: tuple[str, ...] = ("native", "strict_json", "text_protocol")
TOOL_PROTOCOL_THRESHOLDS: dict[str, float] = {"native": 0.95, "strict_json": 0.90}

EDIT_FORMAT_LADDER: tuple[str, ...] = ("unified_diff", "search_replace", "whole_file")
EDIT_FORMAT_THRESHOLDS: dict[str, float] = {"unified_diff": 0.90, "search_replace": 0.85}


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
        tmp = path.with_name(path.name + ".tmp")
        payload = json.dumps(self.model_dump(), indent=2)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
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
            raw = path.read_text(encoding="utf-8")
        except OSError:  # missing file = the normal first boot; unreadable = same outcome
            return None, None
        try:
            return cls.model_validate(json.loads(raw)), None
        except ValueError:  # bad JSON (truncated/empty) or bad schema
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
