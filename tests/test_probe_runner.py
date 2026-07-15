"""Probe runner orchestration (IC-601).

Drives ``run_probes`` / ``evaluate_probes`` / ``probe_and_save`` /
``render_report_card`` with a MockProvider and tiny fake probes. The runner must:
merge dotted-path scores into the profile, degrade a raising or ok=False probe to a
conservative value without aborting the run, and produce a saved, loadable profile plus
a report card that names the recommended protocol/format and the honest context.
"""

import asyncio
from pathlib import Path

from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.runner import (
    ProbeResult,
    evaluate_probes,
    probe_and_save,
    render_report_card,
    run_probes,
)
from ironcore.providers.mock import MockProvider

# --------------------------------------------------------------------------- #
# Tiny fake probes (the real ones land in IC-602/603/604)
# --------------------------------------------------------------------------- #


class FakeProbe:
    """A probe that just returns a canned ProbeResult, ignoring the provider."""

    def __init__(self, probe_id, targets, scores, *, notes="", ok=True):
        self.id = probe_id
        self.title = f"fake {probe_id}"
        self.targets = targets
        self._scores = scores
        self._notes = notes
        self._ok = ok

    async def run(self, provider):
        # touch the provider so a scripted response is consumed, proving wiring
        await provider.complete([])
        return ProbeResult(self.id, dict(self._scores), notes=self._notes, ok=self._ok)


class RaisingProbe:
    id = "BOOM"
    title = "always raises"
    targets = ("tool_protocols.native",)

    async def run(self, provider):
        raise RuntimeError("kaboom")


class RaisingContextProbe:
    """Raises but only targets a context field — must be left at base, not degraded."""

    id = "CTX-BOOM"
    title = "context probe that raises"
    targets = ("honest_context",)

    async def run(self, provider):
        raise ValueError("no needles found")


def _provider(n):
    from ironcore.providers.base import CompletionResult, Message

    entries = [CompletionResult(message=Message(role="assistant", content="ok")) for _ in range(n)]
    return MockProvider(script=entries)


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #


def test_run_probes_merges_dotted_paths():
    probes = [
        FakeProbe("A", ("tool_protocols.native",), {"tool_protocols.native": 0.97}),
        FakeProbe("B", ("edit_formats.unified_diff",), {"edit_formats.unified_diff": 0.93}),
        FakeProbe(
            "C",
            ("honest_context", "json_adherence"),
            {"honest_context": 16384, "json_adherence": 0.88},
        ),
    ]
    profile = asyncio.run(
        run_probes(_provider(3), probes, model_id="m", probed_at="2026-07-15T00:00:00+00:00")
    )
    assert profile.model_id == "m"
    assert profile.probed_at == "2026-07-15T00:00:00+00:00"
    assert profile.tool_protocols["native"] == 0.97
    assert profile.edit_formats["unified_diff"] == 0.93
    assert profile.honest_context == 16384
    assert isinstance(profile.honest_context, int)
    assert profile.json_adherence == 0.88
    # ladders now select the measured rungs
    assert profile.recommended_tool_protocol() == "native"
    assert profile.recommended_edit_format() == "unified_diff"


def test_scalar_int_coercion_from_float():
    probes = [FakeProbe("CTX", ("honest_context",), {"honest_context": 8192.0})]
    profile = asyncio.run(run_probes(_provider(1), probes, model_id="m", probed_at="t"))
    assert profile.honest_context == 8192
    assert isinstance(profile.honest_context, int)


# --------------------------------------------------------------------------- #
# Partial failure tolerance
# --------------------------------------------------------------------------- #


def test_raising_probe_degrades_and_run_continues():
    # BOOM raises; the following probe must still land its score.
    probes = [
        RaisingProbe(),
        FakeProbe("OK", ("edit_formats.search_replace",), {"edit_formats.search_replace": 0.9}),
    ]
    profile = asyncio.run(run_probes(_provider(1), probes, model_id="m", probed_at="t"))
    assert profile.tool_protocols["native"] == 0.0  # degraded, not missing
    assert profile.edit_formats["search_replace"] == 0.9  # run continued
    assert profile.recommended_tool_protocol() == "text_protocol"


def test_ok_false_probe_degrades_reliabilities():
    probes = [
        FakeProbe(
            "SHAKY",
            ("tool_protocols.native",),
            {"tool_protocols.native": 0.99},  # claimed high, but ok=False
            notes="only 3/10 trials parsed",
            ok=False,
        ),
    ]
    profile = asyncio.run(run_probes(_provider(1), probes, model_id="m", probed_at="t"))
    assert profile.tool_protocols["native"] == 0.0


def test_raising_context_probe_leaves_base_default():
    # A failed context measurement must NOT invent a smaller honest_context.
    base = CapabilityProfile(model_id="m", honest_context=4096)
    probes = [RaisingContextProbe()]
    profile = asyncio.run(
        run_probes(_provider(0), probes, model_id="m", base=base, probed_at="t")
    )
    assert profile.honest_context == 4096  # untouched, not degraded to 0


def test_evaluate_probes_records_notes_and_never_raises():
    probes = [RaisingProbe(), FakeProbe("OK", (), {})]
    results = asyncio.run(evaluate_probes(_provider(1), probes))
    assert [r.probe_id for r in results] == ["BOOM", "OK"]
    boom = results[0]
    assert boom.ok is False
    assert "RuntimeError" in boom.notes and "kaboom" in boom.notes
    assert boom.scores == {"tool_protocols.native": 0.0}
    assert results[1].ok is True


def test_ok_false_note_is_preserved():
    probes = [
        FakeProbe(
            "SHAKY", ("json_adherence",), {"json_adherence": 0.8}, notes="distractors won", ok=False
        )
    ]
    results = asyncio.run(evaluate_probes(_provider(1), probes))
    assert results[0].ok is False
    assert "distractors won" in results[0].notes
    assert results[0].scores == {"json_adherence": 0.0}


# --------------------------------------------------------------------------- #
# base handling
# --------------------------------------------------------------------------- #


def test_base_is_deep_copied_not_mutated():
    base = CapabilityProfile(
        model_id="old", tool_protocols={"native": 0.5}, context_window=32768
    )
    probes = [FakeProbe("A", ("tool_protocols.native",), {"tool_protocols.native": 0.96})]
    profile = asyncio.run(
        run_probes(_provider(1), probes, model_id="new", base=base, probed_at="t")
    )
    assert profile.model_id == "new"
    assert profile.tool_protocols["native"] == 0.96
    assert profile.context_window == 32768  # carried over from base
    # base object is untouched
    assert base.model_id == "old"
    assert base.tool_protocols["native"] == 0.5


# --------------------------------------------------------------------------- #
# Report card
# --------------------------------------------------------------------------- #


def test_report_card_shows_recommendations_and_context():
    profile = CapabilityProfile(
        model_id="qwen3-coder:30b",
        probed_at="2026-07-15T00:00:00+00:00",
        context_window=32768,
        honest_context=16384,
        tool_protocols={"native": 0.97},
        edit_formats={"unified_diff": 0.95},
        json_adherence=0.9,
        instruction_retention=0.8,
    )
    card = render_report_card(profile)
    assert "qwen3-coder:30b" in card
    assert "native  (recommended)" in card
    assert "unified_diff  (recommended)" in card
    assert "16,384" in card and "32,768" in card
    assert "usable" in card.lower()
    # every ladder rung is listed
    for rung in ("native", "strict_json", "text_protocol"):
        assert rung in card
    for fmt in ("unified_diff", "search_replace", "whole_file"):
        assert fmt in card


def test_report_card_unprobed_verdict():
    card = render_report_card(CapabilityProfile(model_id="fresh"))
    assert "text_protocol  (recommended)" in card
    assert "whole_file  (recommended)" in card
    assert "unprobed" in card.lower()


def test_report_card_floor_only_verdict_when_probed_low():
    profile = CapabilityProfile(
        model_id="weak", probed_at="t", tool_protocols={"native": 0.1}, edit_formats={}
    )
    card = render_report_card(profile)
    assert "floor-only" in card.lower()


# --------------------------------------------------------------------------- #
# Save round-trip
# --------------------------------------------------------------------------- #


def test_probe_and_save_writes_loadable_profile(tmp_path: Path):
    probes = [
        FakeProbe("A", ("tool_protocols.native",), {"tool_protocols.native": 0.97}),
        FakeProbe("C", ("honest_context",), {"honest_context": 16384}),
    ]
    saved = asyncio.run(
        probe_and_save(
            _provider(2),
            probes,
            model_id="qwen3-coder:30b",
            envelope_dir=tmp_path,
            probed_at="2026-07-15T00:00:00+00:00",
        )
    )
    loaded = CapabilityProfile.load(tmp_path, "qwen3-coder:30b")
    assert loaded is not None
    assert loaded == saved
    assert loaded.tool_protocols["native"] == 0.97
    assert loaded.honest_context == 16384
