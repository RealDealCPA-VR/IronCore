"""OutcomeLedger + deterministic ladder tuning (MS-8): pure/offline unit tests.

The tuner's contract: DOWNGRADE-ONLY, evidence-gated (min samples + a matching
generation stamp + a measured base), pure (the input profile is never mutated),
and always working through the FROZEN thresholds + ``recommended_*`` ladders —
it edits scores, never selects protocols. Persistence mirrors the envelope
cache: a Windows-safe ``<slug>.outcomes.json`` sidecar, corruption-tolerant.
"""

from __future__ import annotations

from pathlib import Path

from ironcore.envelope.outcomes import (
    DECAY_CAP,
    Counter,
    OutcomeLedger,
    apply_tuning,
    generation_stamp,
)
from ironcore.envelope.profile import CapabilityProfile

PROBED_AT = "2026-07-16T00:00:00+00:00"


def _probed(**kw) -> CapabilityProfile:
    base = dict(
        model_id="m",
        source="probed",
        probed_at=PROBED_AT,
        tool_protocols={"native": 0.97, "strict_json": 0.95},
        edit_formats={"unified_diff": 0.95, "search_replace": 0.9},
    )
    base.update(kw)
    return CapabilityProfile(**base)


def _ledger(profile: CapabilityProfile | None = None, **counters) -> OutcomeLedger:
    profile = profile if profile is not None else _probed()
    ledger = OutcomeLedger(model_id=profile.model_id, profile_stamp=generation_stamp(profile))
    for rung, (attempts, failures) in counters.pop("tools", {}).items():
        ledger.tool_protocols[rung] = Counter(attempts=attempts, failures=failures)
    for fmt, (attempts, failures) in counters.pop("edits", {}).items():
        ledger.edit_formats[fmt] = Counter(attempts=attempts, failures=failures)
    for key, value in counters.items():
        setattr(ledger, key, value)
    return ledger


# --------------------------------------------------------------------------- #
# Counter
# --------------------------------------------------------------------------- #


def test_counter_records_and_rates():
    c = Counter()
    assert c.success_rate() == 1.0  # no evidence must never look like failure
    for ok in (True, True, False, True):
        c.record(ok)
    assert (c.attempts, c.failures) == (4, 1)
    assert c.success_rate() == 0.75


def test_counter_decays_by_halving_past_the_cap():
    c = Counter(attempts=DECAY_CAP, failures=60)
    c.record(False)  # crosses the cap -> both halve, ratio roughly preserved
    assert c.attempts == (DECAY_CAP + 1) // 2
    assert c.failures == 61 // 2
    assert 0.0 < c.success_rate() < 1.0


def test_turn_and_verify_counters_decay_too():
    ledger = _ledger(turns=DECAY_CAP, drift_events=100, verify_runs=DECAY_CAP,
                     verify_failures=50)
    ledger.record_turn(drift=True)
    ledger.record_verify(False)
    assert ledger.turns == (DECAY_CAP + 1) // 2
    assert ledger.drift_events == 101 // 2
    assert ledger.verify_runs == (DECAY_CAP + 1) // 2
    assert ledger.verify_failures == 51 // 2


# --------------------------------------------------------------------------- #
# Persistence: sidecar save/load, corruption tolerance
# --------------------------------------------------------------------------- #


def test_save_load_round_trip_writes_the_outcomes_sidecar(tmp_path: Path):
    ledger = _ledger(tools={"native": (12, 3)}, edits={"unified_diff": (9, 4)},
                     turns=5, drift_events=1, verify_runs=2, verify_failures=1)
    path = ledger.save(tmp_path)
    assert path is not None
    assert path.name == f"{CapabilityProfile.slug('m')}.outcomes.json"
    loaded = OutcomeLedger.load(tmp_path, "m")
    assert loaded == ledger
    assert loaded.tool_protocols["native"].attempts == 12


def test_load_missing_returns_a_fresh_ledger_that_can_save(tmp_path: Path):
    ledger = OutcomeLedger.load(tmp_path, "never-seen")
    assert ledger.model_id == "never-seen"
    assert ledger.tool_protocols == {} and ledger.turns == 0
    # the load dir is remembered: a later best-effort save just works
    assert ledger.save() is not None
    assert OutcomeLedger.path_for(tmp_path, "never-seen").exists()


def test_load_corrupt_json_returns_fresh_and_never_raises(tmp_path: Path):
    OutcomeLedger.path_for(tmp_path, "m").parent.mkdir(parents=True, exist_ok=True)
    OutcomeLedger.path_for(tmp_path, "m").write_text("{not json", encoding="utf-8")
    ledger = OutcomeLedger.load(tmp_path, "m")
    assert ledger.tool_protocols == {}
    OutcomeLedger.path_for(tmp_path, "m").write_text('{"model_id": 42}', encoding="utf-8")
    assert OutcomeLedger.load(tmp_path, "m").model_id == "m"  # schema-bad -> fresh


def test_save_without_a_directory_is_a_noop():
    assert OutcomeLedger(model_id="m").save() is None


def test_for_model_returns_self_or_loads_the_other_sidecar(tmp_path: Path):
    other = _ledger(_probed(model_id="other"), tools={"native": (5, 5)})
    other.model_id = "other"
    other.save(tmp_path)
    ledger = OutcomeLedger.load(tmp_path, "m")
    assert ledger.for_model("m") is ledger
    swapped = ledger.for_model("other")
    assert swapped.model_id == "other"
    assert swapped.tool_protocols["native"].attempts == 5
    # no remembered dir -> a fresh in-memory ledger, never a crash
    bare = OutcomeLedger(model_id="a")
    assert bare.for_model("b").model_id == "b"


# --------------------------------------------------------------------------- #
# Generation stamps: reset on change, invariant under tuning
# --------------------------------------------------------------------------- #


def test_ensure_stamp_preserves_counters_on_match_and_resets_on_change():
    ledger = _ledger(tools={"native": (20, 4)}, turns=9, drift_events=3,
                     verify_runs=4, verify_failures=2)
    assert ledger.ensure_stamp(generation_stamp(_probed())) is False  # match
    assert ledger.tool_protocols["native"].attempts == 20
    fresh = _probed(probed_at="2026-07-17T00:00:00+00:00")  # a NEW probe landed
    assert ledger.ensure_stamp(generation_stamp(fresh)) is True
    assert ledger.tool_protocols == {} and ledger.edit_formats == {}
    assert ledger.turns == 0 and ledger.drift_events == 0
    assert ledger.verify_runs == 0 and ledger.verify_failures == 0
    assert ledger.profile_stamp == generation_stamp(fresh)


def test_generation_stamp_is_invariant_under_tuning():
    profile = _probed()
    ledger = _ledger(tools={"native": (20, 4)})
    tuned = apply_tuning(profile, ledger).profile
    assert tuned.source == "tuned"
    assert generation_stamp(tuned) == generation_stamp(profile)  # no self-reset


def test_generation_stamp_distinguishes_default_seeded_and_probes():
    default = CapabilityProfile(model_id="m")
    seeded = CapabilityProfile(model_id="m", source="seeded")
    assert generation_stamp(default) != generation_stamp(seeded)
    assert generation_stamp(_probed()) != generation_stamp(seeded)
    reprobed = _probed(probed_at="2026-07-18T00:00:00+00:00")
    assert generation_stamp(_probed()) != generation_stamp(reprobed)


# --------------------------------------------------------------------------- #
# apply_tuning: the downgrade-only rules
# --------------------------------------------------------------------------- #


def test_below_min_samples_changes_nothing():
    profile = _probed()
    result = apply_tuning(profile, _ledger(tools={"native": (5, 5)}))
    assert result.profile == profile
    assert result.profile.source == "probed"
    assert result.adjustments == []


def test_failing_live_rate_lowers_the_score_and_flips_the_frozen_ladder():
    profile = _probed()
    assert profile.recommended_tool_protocol() == "native"
    result = apply_tuning(profile, _ledger(tools={"native": (20, 4)}))  # rate 0.80
    tuned = result.profile
    assert tuned.tool_protocols["native"] == 0.80  # min(stored 0.97, live 0.80)
    assert tuned.recommended_tool_protocol() == "strict_json"  # the LADDER decided
    assert tuned.source == "tuned"
    assert tuned.probed_at == PROBED_AT  # the base measurement stands
    assert result.adjustments and "native" in result.adjustments[0]


def test_live_rate_above_the_stored_score_never_raises_it():
    profile = _probed(tool_protocols={"native": 0.5, "strict_json": 0.95})
    # native stored 0.5 (below threshold): clean live evidence must NOT promote it
    result = apply_tuning(profile, _ledger(profile, tools={"native": (50, 0)}))
    assert result.profile.tool_protocols["native"] == 0.5
    assert result.adjustments == []


def test_edit_format_ladder_tunes_identically():
    profile = _probed()
    assert profile.recommended_edit_format() == "unified_diff"
    result = apply_tuning(profile, _ledger(edits={"unified_diff": (8, 4)}))  # rate 0.5
    tuned = result.profile
    assert tuned.edit_formats["unified_diff"] == 0.5
    assert tuned.recommended_edit_format() == "search_replace"
    assert tuned.source == "tuned"


def test_rung_already_below_threshold_is_not_double_downgraded():
    profile = _probed(tool_protocols={"native": 0.5, "strict_json": 0.95})
    result = apply_tuning(profile, _ledger(profile, tools={"native": (20, 10)}))
    assert result.profile.tool_protocols["native"] == 0.5  # untouched
    assert result.adjustments == []


def test_drift_ratio_at_boundary_lowers_the_coherence_horizon():
    profile = _probed(coherence_horizon=6)
    result = apply_tuning(profile, _ledger(turns=8, drift_events=2))  # exactly 0.25
    assert result.profile.coherence_horizon == 4
    assert result.profile.source == "tuned"
    assert result.profile.anchor_cadence() == 4  # still inside the frozen [2, 12]


def test_drift_ratio_below_boundary_changes_nothing():
    result = apply_tuning(_probed(coherence_horizon=6), _ledger(turns=25, drift_events=6))
    assert result.profile.coherence_horizon == 6  # 0.24 < 0.25
    assert result.adjustments == []


def test_coherence_horizon_clamps_at_two():
    result = apply_tuning(_probed(coherence_horizon=3), _ledger(turns=8, drift_events=8))
    assert result.profile.coherence_horizon == 2
    again = apply_tuning(result.profile, _ledger(result.profile, turns=8, drift_events=8))
    assert again.profile.coherence_horizon == 2  # never below the clamp
    assert again.adjustments == []  # ... and no phantom adjustment note


def test_perfect_live_rate_emits_a_reprobe_hint_and_edits_nothing():
    profile = _probed(tool_protocols={"native": 0.0, "strict_json": 0.95})
    assert profile.recommended_tool_protocol() == "strict_json"
    result = apply_tuning(profile, _ledger(profile, tools={"strict_json": (100, 0)}))
    assert result.profile == profile  # byte-identical: upgrades are NEVER applied
    assert result.profile.source == "probed"
    assert result.reprobe_hints and "/probe" in result.reprobe_hints[0]
    assert "native" in result.reprobe_hints[0]


def test_input_profile_is_never_mutated():
    profile = _probed()
    snapshot = profile.model_copy(deep=True)
    apply_tuning(profile, _ledger(tools={"native": (20, 20)}, edits={"unified_diff": (8, 8)},
                                  turns=8, drift_events=8))
    assert profile == snapshot


def test_stamp_mismatch_returns_the_input_unchanged():
    profile = _probed()
    ledger = _ledger(tools={"native": (20, 20)})
    ledger.profile_stamp = "measured:some-older-probe"
    result = apply_tuning(profile, ledger)
    assert result.profile == profile
    assert result.adjustments == [] and result.reprobe_hints == []


def test_other_models_ledger_is_ignored():
    profile = _probed()
    ledger = _ledger(tools={"native": (20, 20)})
    ledger.model_id = "someone-else"
    assert apply_tuning(profile, ledger).profile == profile


def test_unmeasured_profiles_are_never_tuned():
    floor = CapabilityProfile(model_id="m")  # source=default, probed_at=None
    ledger = OutcomeLedger(model_id="m", profile_stamp=generation_stamp(floor))
    ledger.turns, ledger.drift_events = 8, 8
    result = apply_tuning(floor, ledger)
    assert result.profile == floor
    assert result.adjustments == []


def test_tuning_a_tuned_profile_is_stable():
    # boot N tunes the DISK profile; re-running on the tuned copy (same ledger)
    # must not compound: the score is already below threshold.
    profile = _probed()
    ledger = _ledger(tools={"native": (20, 4)})
    first = apply_tuning(profile, ledger).profile
    second = apply_tuning(first, ledger)
    assert second.profile.tool_protocols["native"] == first.tool_protocols["native"]
    assert second.adjustments == []
