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
