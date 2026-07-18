"""render_report_card must label a profile's provenance honestly.

The card tells the user whether the numbers are guesses (seeded from endpoint
introspection, still measuring), measurements (the deep probe ran), or floor
defaults (nothing known yet). A seeded profile has ``probed_at is None`` like a
default one but is NOT floor-only -- ``source`` is what distinguishes them.
The card must stay ASCII-safe for the Windows console (no em-dash/ellipsis).

The styled sibling ``render_report_card_text`` is pinned here too, on two
properties: its plain text is byte-identical to the string card (so a pipe, a
non-TTY consumer and a pasted GitHub issue get exactly what these tests assert),
and it never lets a model id become Rich console markup.
"""

import io

from rich.console import Console
from rich.text import Text

from ironcore.envelope.profile import CapabilityProfile
from ironcore.envelope.runner import render_report_card, render_report_card_text


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


def test_card_shows_the_measured_token_ratio():
    card = render_report_card(
        CapabilityProfile(
            model_id="m", source="probed", probed_at="t", chars_per_token=3.2
        )
    )
    assert "3.2 chars/token" in card
    assert card.isascii()
    ratio_line = next(line for line in card.splitlines() if "chars/token" in line)
    assert "default" not in ratio_line  # a measured ratio is not labelled default


def test_card_labels_the_default_ratio_honestly():
    card = render_report_card(CapabilityProfile(model_id="fresh"))
    assert "4.0 chars/token" in card
    assert "(default)" in card
    assert card.isascii()


# -- source == "tuned" (MS-8): measured, then lowered from live evidence --------


def _tuned() -> CapabilityProfile:
    # what apply_tuning emits: a probed base whose native score live evidence
    # lowered below its threshold — the frozen ladder now picks strict_json.
    return CapabilityProfile(
        model_id="qwen3-coder:30b",
        source="tuned",
        probed_at="2026-07-15T00:00:00+00:00",
        tool_protocols={"native": 0.80, "strict_json": 0.95},
        edit_formats={"unified_diff": 0.95},
    )


def test_tuned_card_says_tuned_and_shows_the_lowered_ladders():
    card = render_report_card(_tuned())
    lower = card.lower()
    assert "tuned" in lower
    assert "live-session evidence" in lower
    assert "/probe" in card  # the honest way back up the ladders
    assert "strict_json" in card and "unified_diff" in card  # the ACTIVE rungs
    assert "unprobed" not in lower  # a tuned profile is measured, never floor-default
    assert "seeded" not in lower
    assert card.isascii()


def test_tuned_card_keeps_the_probe_timestamp():
    card = render_report_card(_tuned())
    assert "2026-07-15T00:00:00+00:00" in card  # the base measurement stands


# -- vision line (MS-6) ---------------------------------------------------------


def test_card_shows_vision_no_by_default():
    card = render_report_card(CapabilityProfile(model_id="fresh"))
    vision_line = next(line for line in card.splitlines() if line.startswith("Vision:"))
    assert "no" in vision_line
    assert card.isascii()


def test_card_shows_vision_yes_when_the_profile_has_it():
    card = render_report_card(CapabilityProfile(model_id="llava", vision=True))
    vision_line = next(line for line in card.splitlines() if line.startswith("Vision:"))
    assert "yes" in vision_line
    assert card.isascii()


# -- the styled card (CONTRACTS.md §6) ------------------------------------------


def _render(text: Text) -> str:
    """The card as a real terminal would receive it: escape codes and all."""
    buffer = io.StringIO()
    Console(
        file=buffer,
        width=120,
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
    ).print(text)
    return buffer.getvalue()


def _profiles() -> list[CapabilityProfile]:
    return [
        _seeded_native(),
        _tuned(),
        CapabilityProfile(model_id="fresh"),
        CapabilityProfile(
            model_id="qwen3-coder:30b",
            source="probed",
            probed_at="2026-07-15T00:00:00+00:00",
            context_window=262144,
            honest_context=49152,
            tool_protocols={"native": 0.98, "strict_json": 0.94},
            edit_formats={"unified_diff": 0.71, "search_replace": 0.93},
        ),
    ]


def test_styled_card_plain_text_is_the_string_card_exactly():
    """One builder, two views. The plain-text path (a pipe, a non-TTY consumer,
    a card pasted into an issue) must not drift from the coloured one by a
    single character — which is what makes every pin above cover both."""
    for profile in _profiles():
        assert render_report_card_text(profile).plain == render_report_card(profile)


def test_styled_card_colours_the_ladder_verdicts():
    """Colour reinforces the words; it must actually be there. SELECTED and
    REJECTED must not render identically — that was the whole bug."""
    profile = _profiles()[-1]
    card = render_report_card_text(profile)
    styles = {
        span_text: str(span.style)
        for span in card.spans
        if (span_text := card.plain[span.start : span.end])
    }
    assert "bold" in styles["SELECTED"]
    assert styles["SELECTED"] != styles["REJECTED (0.19 short)"]
    # and the accessible carrier survives with no colour at all
    plain = card.plain
    assert "SELECTED" in plain and "REJECTED (0.19 short)" in plain


def test_unmeasured_cards_carry_no_success_colour():
    """Green means "a measurement cleared a bar" — and on a card whose whole job
    is telling guesses from evidence, it must never mean anything else.

    An unprobed profile selects the FLOOR rung (nothing cleared anything) and a
    seeded one only read the endpoint's own claim back, so neither may show the
    success colour anywhere: not on the ladder heading, not on the SELECTED
    marker, not on the honest/advertised ratio. They render amber and grey, and
    the Source line says why.
    """
    from ironcore.term import SUCCESS

    # the palette's success green as this truecolor console emits it
    green = "38;2;" + ";".join(str(int(SUCCESS[i : i + 2], 16)) for i in (1, 3, 5))

    for profile in (
        CapabilityProfile(model_id="fresh"),
        CapabilityProfile(model_id="s", source="seeded", context_window=8192,
                          honest_context=8192, tool_protocols={"native": 0.95}),
    ):
        assert green not in _render(render_report_card_text(profile))
    # ...whereas a genuinely measured one does earn it
    assert green in _render(render_report_card_text(_profiles()[-1]))


def test_report_card_never_interprets_markup():
    """SAFETY: a model id is endpoint/config data, and the transcript's whole
    reason for wrapping dynamic text in ``Text`` is that such data can never be
    reinterpreted as Rich console markup. A model called ``[red]evil[/]`` must
    print those characters and must not arm a colour.
    """
    evil = "[red]evil[/] [bold]x[/bold] [/]"
    card = render_report_card_text(CapabilityProfile(model_id=evil))

    assert evil in card.plain  # survives verbatim into the plain view
    rendered = _render(card)
    assert evil in rendered  # ...and into a real terminal, tags and all
    # Rich's own `red` is SGR 31; this palette only ever emits truecolor
    # (38;2;r;g;b), so 31m appearing at all would mean the markup was parsed.
    assert "\x1b[31m" not in rendered
    assert "\x1b[1m" not in rendered  # nor `[bold]`


def test_report_card_markup_in_every_field_stays_literal():
    """Not just the model id: every string that reaches the card from outside."""
    evil = "[red]pwn[/]"
    card = render_report_card_text(
        CapabilityProfile(model_id=evil, source="probed", probed_at=evil)
    )
    assert card.plain.count(evil) == 2
    rendered = _render(card)
    assert rendered.count(evil) == 2
    assert "\x1b[31m" not in rendered
