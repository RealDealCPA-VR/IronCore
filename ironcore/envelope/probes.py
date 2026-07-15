"""Probe suite: how the envelope gets measured — TODO IC-602..IC-604.

Each probe is a short, deterministic-as-possible trial battery run against
a model on first use (~2 minutes total, cached). Probes must be cheap,
repeatable (fixed seeds where the server supports them), and scored
mechanically — a probe that needs an LLM judge is a design smell here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeSpec:
    id: str
    title: str
    measures: str  # which CapabilityProfile field(s) this fills
    task: str  # TODO.md task that implements it


PROBES: tuple[ProbeSpec, ...] = (
    ProbeSpec(
        id="CTX-HONESTY",
        title="Needle retrieval at increasing depths",
        measures="honest_context (depth where retrieval stays >= 0.9)",
        task="IC-602",
    ),
    ProbeSpec(
        id="RETENTION",
        title="Constraint given at turn 1, tested at turn k",
        measures="instruction_retention, coherence_horizon",
        task="IC-602",
    ),
    ProbeSpec(
        id="TOOL-FORM",
        title="N tool-call trials per wire protocol",
        measures="tool_protocols[native|strict_json|text_protocol]",
        task="IC-603",
    ),
    ProbeSpec(
        id="JSON-STRICT",
        title="Schema-conforming JSON emission under pressure",
        measures="json_adherence",
        task="IC-603",
    ),
    ProbeSpec(
        id="EDIT-FORMAT",
        title="Produce edits in each format; harness applies to fixtures",
        measures="edit_formats[unified_diff|search_replace|whole_file]",
        task="IC-604",
    ),
    ProbeSpec(
        id="CODE-SMOKE",
        title="Small function + failing test -> green",
        measures="sanity gate; flags models below the usability floor",
        task="IC-604",
    ),
)
