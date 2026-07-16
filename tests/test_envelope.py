"""Adapter ladders and profile persistence."""

from pathlib import Path

from ironcore.envelope.profile import CapabilityProfile


def test_native_when_reliable():
    profile = CapabilityProfile(model_id="m", tool_protocols={"native": 0.98})
    assert profile.recommended_tool_protocol() == "native"


def test_downgrade_to_strict_json():
    profile = CapabilityProfile(
        model_id="m", tool_protocols={"native": 0.7, "strict_json": 0.93}
    )
    assert profile.recommended_tool_protocol() == "strict_json"


def test_text_protocol_is_the_floor():
    profile = CapabilityProfile(model_id="m", tool_protocols={"native": 0.5, "strict_json": 0.4})
    assert profile.recommended_tool_protocol() == "text_protocol"
    assert CapabilityProfile(model_id="unprobed").recommended_tool_protocol() == "text_protocol"


def test_edit_format_ladder():
    strong = CapabilityProfile(model_id="m", edit_formats={"unified_diff": 0.95})
    mid = CapabilityProfile(model_id="m", edit_formats={"unified_diff": 0.6, "search_replace": 0.9})
    weak = CapabilityProfile(model_id="m")
    assert strong.recommended_edit_format() == "unified_diff"
    assert mid.recommended_edit_format() == "search_replace"
    assert weak.recommended_edit_format() == "whole_file"


def test_anchor_cadence_bounded():
    assert CapabilityProfile(model_id="m", coherence_horizon=1).anchor_cadence() == 2
    assert CapabilityProfile(model_id="m", coherence_horizon=50).anchor_cadence() == 12
    assert CapabilityProfile(model_id="m", coherence_horizon=6).anchor_cadence() == 6


def test_save_load_roundtrip(tmp_path: Path):
    profile = CapabilityProfile(
        model_id="qwen3-coder:30b",
        probed_at="2026-07-15T00:00:00+00:00",
        tool_protocols={"native": 0.97},
        json_adherence=0.91,
    )
    profile.save(tmp_path)
    loaded = CapabilityProfile.load(tmp_path, "qwen3-coder:30b")
    assert loaded == profile


def test_load_missing_returns_none(tmp_path: Path):
    assert CapabilityProfile.load(tmp_path, "never-probed") is None


def test_slug_is_filesystem_safe():
    assert "/" not in CapabilityProfile.slug("org/model:7b-q4_K_M")
    assert ":" not in CapabilityProfile.slug("org/model:7b-q4_K_M")


# -- chars_per_token (MS-1): persistence + legacy-cache compatibility -----------


def test_chars_per_token_round_trips(tmp_path: Path):
    profile = CapabilityProfile(model_id="m", chars_per_token=3.2)
    profile.save(tmp_path)
    loaded = CapabilityProfile.load(tmp_path, "m")
    assert loaded is not None
    assert loaded.chars_per_token == 3.2


def test_legacy_envelope_json_without_ratio_loads_as_default(tmp_path: Path):
    # a cached envelope written BEFORE MS-1 has no chars_per_token key
    import json

    legacy = CapabilityProfile(model_id="legacy-model").model_dump()
    legacy.pop("chars_per_token")
    path = tmp_path / f"{CapabilityProfile.slug('legacy-model')}.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = CapabilityProfile.load(tmp_path, "legacy-model")
    assert loaded is not None
    assert loaded.chars_per_token == 4.0  # pydantic default: byte-identical legacy packing
