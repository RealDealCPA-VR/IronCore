"""render_report_card must label a profile's provenance honestly.

The card tells the user whether the numbers are guesses (seeded from endpoint
introspection, still measuring), measurements (the deep probe ran), or floor
defaults (nothing known yet). A seeded profile has ``probed_at is None`` like a
default one but is NOT floor-only -- ``source`` is what distinguishes them.
The card must stay ASCII-safe for the Windows console (no em-dash/ellipsis).
"""

from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.runner import render_report_card


def _seeded_native() -> CapabilityProfile:
    # what seed_profile emits when the endpoint advertises native tool-calling
    return CapabilityProfile(
        model_id="qwen3-coder:30b",
        source="seeded",
        probed_at=None,
        context_window=32768,
        honest_context=32768,
        tool_protocols={"native": 0.95},
        edit_formats={"search_replace": 0.85},
    )


def test_seeded_card_is_provisional_and_shows_seed_ladders():
    card = render_report_card(_seeded_native())
    lower = card.lower()
    assert "seeded" in lower
    assert "provisional" in lower
    # the provisional verdict reflects the SEED's recommended rungs, not the floor
    assert "native" in card
    assert "search_replace" in card
    assert "unprobed" not in lower  # a seed is NOT the floor default
    assert card.isascii()


def test_seeded_without_native_signal_is_honest_floor_but_still_seeded():
    # detect() found no native tools -> empty ladders -> floor rungs, but still seeded
    card = render_report_card(
        CapabilityProfile(model_id="m", source="seeded", probed_at=None)
    )
    lower = card.lower()
    assert "seeded" in lower
    assert "provisional" in lower
    assert "measuring in the background" in lower
    assert card.isascii()


def test_probed_card_says_measured():
    card = render_report_card(
        CapabilityProfile(
            model_id="qwen3-coder:30b",
            source="probed",
            probed_at="2026-07-15T00:00:00+00:00",
            context_window=32768,
            honest_context=16384,
            tool_protocols={"native": 0.97},
            edit_formats={"unified_diff": 0.95},
        )
    )
    lower = card.lower()
    assert "measured" in lower
    assert "usable" in lower
    assert "seeded" not in lower
    assert card.isascii()


def test_default_card_says_defaults_and_unprobed():
    card = render_report_card(CapabilityProfile(model_id="fresh"))
    lower = card.lower()
    assert "defaults" in lower
    assert "unprobed" in lower
    assert "seeded" not in lower
    # the Source line must not claim measurement (the verdict's "until measured" is fine)
    assert "source:" in lower and "measured" not in lower.split("verdict:")[0]
    assert card.isascii()
