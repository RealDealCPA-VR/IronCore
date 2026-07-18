"""Probe runner: orchestrate a set of probes into a CapabilityProfile + report card.

IC-601 owns the ORCHESTRATION only. The individual probe implementations land in
IC-602 (CTX-HONESTY, RETENTION), IC-603 (TOOL-FORM, JSON-STRICT), and IC-604
(EDIT-FORMAT, CODE-SMOKE). This module defines the ``Probe`` interface those tasks
implement and the machinery that folds their results into a saved profile + a report
card (rendered by ``/envelope``, IC-608).

Runner rules (SPEC §4.1, MODELS §2):
  * Partial failure never aborts the run. A probe that RAISES or returns ``ok=False``
    degrades its declared *reliability* targets to a conservative ``0.0`` and records a
    note; *context/horizon* targets are left at the base/default (a failed measurement
    must not invent a smaller honest_context). Every other probe still runs.
  * Deterministic + offline. No ``datetime.now``, no network, no disk writes inside
    ``run_probes``: the caller stamps ``probed_at`` and chooses ``envelope_dir``. Probes
    talk to a ``Provider`` (``MockProvider`` in tests), nothing else.
  * The runner never *picks* protocols — it only *fills* fields. Protocol/format
    selection stays in ``CapabilityProfile.recommended_*`` (frozen, CONTRACTS.md §5).

The dotted-path merge convention (CRITICAL — probe authors implement this exactly):
  ``ProbeResult.scores`` maps a *dotted profile path* to the value to merge in place:
    * dict fields   -> ``"tool_protocols.native"``, ``"edit_formats.unified_diff"``,
                       ``"sampling.temperature"``   (value written under that sub-key)
    * scalar fields -> ``"honest_context"``, ``"context_window"``,
                       ``"coherence_horizon"``       (coerced to int)
                       ``"json_adherence"``, ``"instruction_retention"``,
                       ``"chars_per_token"``         (coerced to float)
  A path whose root is ``tool_protocols`` / ``edit_formats`` / ``json_adherence`` /
  ``instruction_retention`` is a RELIABILITY (degrades to ``0.0`` on failure).
  ``honest_context`` / ``context_window`` / ``coherence_horizon`` / ``sampling`` /
  ``chars_per_token`` are NOT reliabilities — a failed probe leaves them at the
  base/default (a failed measurement must not invent a token ratio). Unknown or malformed
  paths from a misbehaving probe are skipped, never fatal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from rich.text import Text

from ironcore import term
from ironcore.envelope.profile import (
    EDIT_FORMAT_LADDER,
    EDIT_FORMAT_THRESHOLDS,
    TOOL_PROTOCOL_LADDER,
    TOOL_PROTOCOL_THRESHOLDS,
    CapabilityProfile,
)
from ironcore.providers.base import Provider

# --------------------------------------------------------------------------- #
# Field taxonomy for the dotted-path merge (see module docstring)
# --------------------------------------------------------------------------- #

_DICT_FIELDS = frozenset({"tool_protocols", "edit_formats", "sampling"})
_INT_FIELDS = frozenset({"honest_context", "context_window", "coherence_horizon"})
#: chars_per_token is deliberately NOT in _RELIABILITY_ROOTS: a failed TOKEN-RATIO
#: measurement keeps the 4.0 base instead of degrading to a nonsense 0.0 ratio.
_FLOAT_FIELDS = frozenset({"json_adherence", "instruction_retention", "chars_per_token"})
#: Roots whose scores are reliabilities in [0, 1] and degrade to 0.0 on failure.
_RELIABILITY_ROOTS = frozenset(
    {"tool_protocols", "edit_formats", "json_adherence", "instruction_retention"}
)


# --------------------------------------------------------------------------- #
# The interface IC-602/603/604 implement
# --------------------------------------------------------------------------- #


@dataclass
class ProbeResult:
    """What one probe reports back to the runner.

    * ``probe_id``   — the probe's stable id (matches ``Probe.id`` / a PROBES entry).
    * ``scores``     — dotted-path -> value, merged into the profile (see module docstring).
    * ``notes``      — human-readable diagnostics for the report / transcript.
    * ``ok``         — False => the probe ran but did not trust its own result; the runner
                       degrades this probe's reliability targets to 0.0.
    """

    probe_id: str
    scores: dict[str, float] = field(default_factory=dict)
    notes: str = ""
    ok: bool = True


class Probe(Protocol):
    """A single capability probe. IC-602/603/604 supply concrete classes.

    Attributes:
      * ``id``      — stable identifier (e.g. ``"TOOL-FORM"``).
      * ``title``   — one-line human description.
      * ``targets`` — the dotted profile paths this probe fills. The runner uses these to
        degrade the right fields when the probe RAISES (no ProbeResult is produced then),
        so declaring them accurately is part of the contract.

    ``run`` performs the trials against ``provider`` and returns a ``ProbeResult``. It may
    raise on catastrophic failure — the runner tolerates that (partial-failure rule).
    """

    id: str
    title: str
    targets: Sequence[str]

    async def run(self, provider: Provider) -> ProbeResult: ...


# --------------------------------------------------------------------------- #
# Merge + degrade helpers
# --------------------------------------------------------------------------- #


def _root(path: str) -> str:
    return path.partition(".")[0]


def _is_reliability_path(path: str) -> bool:
    return _root(path) in _RELIABILITY_ROOTS


def _degraded_scores(targets: Sequence[str]) -> dict[str, float]:
    """Conservative floor for a failed probe: reliability targets -> 0.0; context and
    horizon targets are omitted so they keep the base/default value."""
    return {t: 0.0 for t in targets if _is_reliability_path(t)}


def _merge_score(profile: CapabilityProfile, path: str, value: float) -> None:
    """Write one dotted-path score into ``profile`` in place. Unknown or malformed paths
    are skipped — a probe-author bug must never abort the run."""
    head, _, tail = path.partition(".")
    if head in _DICT_FIELDS and tail:
        getattr(profile, head)[tail] = float(value)
    elif head in _INT_FIELDS and not tail:
        setattr(profile, head, int(value))
    elif head in _FLOAT_FIELDS and not tail:
        setattr(profile, head, float(value))
    # anything else: silently ignored (see module docstring)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


async def evaluate_probes(provider: Provider, probes: Sequence[Probe]) -> list[ProbeResult]:
    """Run every probe against ``provider``, tolerating partial failure.

    A probe that raises or returns ``ok=False`` is converted into a degraded
    ``ProbeResult`` (reliability targets -> 0.0, a note recorded); the run always
    completes. This is the layer that carries per-probe notes — ``run_probes`` folds the
    scores into a profile and drops the notes (the profile has no notes field, so
    ``/envelope`` reads notes from a live run, not the cached profile).
    """
    results: list[ProbeResult] = []
    for probe in probes:
        targets = tuple(getattr(probe, "targets", ()))
        probe_id = getattr(probe, "id", "unknown")
        try:
            result = await probe.run(provider)
        except Exception as exc:  # noqa: BLE001 — partial-failure tolerance is the point
            results.append(
                ProbeResult(
                    probe_id=probe_id,
                    scores=_degraded_scores(targets),
                    notes=(
                        f"probe raised {type(exc).__name__}: {exc}; "
                        "reliabilities degraded to 0.0"
                    ),
                    ok=False,
                )
            )
            continue
        if not result.ok:
            degraded = _degraded_scores(targets or tuple(result.scores))
            base_note = result.notes or "probe reported ok=False"
            results.append(
                ProbeResult(
                    probe_id=result.probe_id,
                    scores=degraded,
                    notes=f"{base_note}; reliabilities degraded to 0.0",
                    ok=False,
                )
            )
            continue
        results.append(result)
    return results


async def run_probes(
    provider: Provider,
    probes: Sequence[Probe],
    *,
    model_id: str,
    base: CapabilityProfile | None = None,
    probed_at: str | None = None,
) -> CapabilityProfile:
    """Run ``probes`` and merge their scores into a fresh CapabilityProfile.

    Starts from ``base`` (deep-copied, never mutated) or floor defaults, stamps
    ``model_id`` + ``probed_at`` (the caller supplies the timestamp — this module is
    deterministic), and folds in every ProbeResult via the dotted-path convention. Does
    NOT write to disk (see ``probe_and_save``) and never aborts on a probe failure (see
    ``evaluate_probes``).
    """
    if base is not None:
        profile = base.model_copy(deep=True)
        profile.model_id = model_id
    else:
        profile = CapabilityProfile(model_id=model_id)
    profile.probed_at = probed_at
    profile.source = "probed"  # a measured profile, whatever base it refined

    for result in await evaluate_probes(provider, probes):
        for path, value in result.scores.items():
            _merge_score(profile, path, value)
    return profile


async def probe_and_save(
    provider: Provider,
    probes: Sequence[Probe],
    *,
    model_id: str,
    envelope_dir: Path,
    probed_at: str | None = None,
    base: CapabilityProfile | None = None,
) -> CapabilityProfile:
    """``run_probes`` then persist via ``CapabilityProfile.save``.

    Returns the saved profile; reload it with
    ``CapabilityProfile.load(envelope_dir, model_id)``.
    """
    profile = await run_probes(
        provider, probes, model_id=model_id, base=base, probed_at=probed_at
    )
    profile.save(envelope_dir)
    return profile


# --------------------------------------------------------------------------- #
# Report card (consumed by /envelope, IC-608)
# --------------------------------------------------------------------------- #
#
# ONE builder, two views. ``render_report_card_text`` composes a ``rich.text.Text``
# and ``render_report_card`` is its ``.plain`` — so the coloured card a TUI shows
# and the ASCII card a pipe, a ``doctor``-style CLI run or a pasted GitHub issue
# gets are, by construction, the same characters. They cannot drift, and the
# plain-text pins in tests/test_report_card.py keep guarding both.
#
# SAFETY: every segment is appended with an explicit style. Nothing here goes
# near ``Text.from_markup``, because ``model_id`` (and the routed-role ids the
# command appends after this card) come from a config file or a model endpoint —
# a model called ``[red]evil[/]`` must print those characters, not arm a colour.
# ``tests/test_report_card.py::test_report_card_never_interprets_markup`` pins it.
#
# The palette is ``ironcore/term.py``'s — the same values ``tui/theme.py`` holds
# (tests/test_term.py pins the two together). The card is the one renderable that
# shows up on BOTH sides of that line, so it borrows the leaf module rather than
# inventing a third palette.

#: Labels and thresholds are chrome; values are content; only outcomes take a hue.
_S_LABEL = term.MUTED
_S_VALUE = term.FOREGROUND
_S_HEADING = f"bold {term.FOREGROUND}"
_S_MUTED = term.MUTED
#: The rung that was taken, and a verdict that means "this model can work".
_S_GOOD = f"bold {term.SUCCESS}"
#: A rung that cleared its bar but lost to a better one. Its VERDICT gets a calm
#: steel, but its name and score stay chrome: the first draft gave the fallback
#: row full-brightness foreground, which made it out-shout the SELECTED row it
#: lost to on the two left-hand columns. A ladder that ranks its rows wrong is
#: worse than one that does not rank them at all.
_S_FALLBACK = term.SECONDARY
#: A rung that measured short, and the shortfall itself.
_S_BAD = f"bold {term.ERROR}"
_S_BAD_DIM = term.ERROR
#: Provisional / unmeasured / floor-only: honest, but not yet earned.
_S_WARN = f"bold {term.WARNING}"

#: Fraction of the ADVERTISED window the model actually holds coherently. The
#: gap between those two numbers is this product's opening argument, so it is
#: the one field on the card that is graded rather than merely printed.
_CONTEXT_GOOD = 0.75
_CONTEXT_FAIR = 0.40

#: Width of the label column ("Model:", "Verdict:", …) — the card's spine.
_LABEL_WIDTH = 18
#: Width of the rung-name column, and of the score column beside it.
_RUNG_WIDTH = 15
_SCORE_WIDTH = 4


def _row(label: str, value: str, style: str = _S_VALUE) -> Text:
    """One ``label   value`` line of the card's header/footer blocks."""
    text = Text()
    text.append(f"{label:<{_LABEL_WIDTH}}", style=_S_LABEL)
    text.append(value, style=style)
    return text


def _selected_style(
    profile: CapabilityProfile, rung: str, thresholds: dict[str, float]
) -> str:
    """The colour a SELECTED rung earns.

    Green means one thing on this card, exactly: **a real measurement cleared a
    bar.** Two cases therefore do not get it, and the rule is worth stating in
    one place because a reader learns it from the whole card at once:

    * The FLOOR rung clears nothing — it is the safety net, chosen because
      everything above it was rejected or never probed. An unprobed model would
      otherwise open with a green ``text_protocol`` heading and a green
      ``SELECTED`` on the floor, which reads as good news on exactly the profile
      that has no news at all.
    * A rung selected on a SEEDED profile cleared its bar with an *introspected*
      score — the endpoint's claim about itself, not a trial. Real signal, but
      not evidence, and this card's whole job is telling those apart.

    So a card is either measured (some green) or provisional (amber and grey),
    and the Source line above says which. 🔒 tests/test_report_card.py
    """
    return _S_GOOD if rung in thresholds and _is_measured(profile) else _S_WARN


def _rung_text(
    profile: CapabilityProfile,
    rung: str,
    scores: dict[str, float],
    thresholds: dict[str, float],
    recommended: str,
) -> Text:
    """One ladder rung as a four-column row: marker, rung, score, verdict.

    The ladder is the product's whole thesis, so a reader must be able to see
    which rung was TAKEN and which were refused without decoding two numbers per
    line. The old row said ``0.71  (needs >= 0.90, below)`` and left "below" as
    the only, easily-missed signal that the rung was thrown out. Now the outcome
    is a word in its own column — ``SELECTED`` / ``REJECTED`` / ``ok, fallback``
    — a rejection says how far short it fell, and the word carries a colour.

    The words and the columns are load-bearing on their own; colour only
    reinforces them (``tests/test_report_card.py`` pins the plain text). The card
    is printed into a Windows console and pasted into issues, so its structure
    has to survive with no colour and no box-drawing at all — which is why the
    verdict is a WORD in a column and not, say, a green bullet.
    """
    marker = "->" if rung == recommended else "  "
    selected = rung == recommended
    chosen_style = _selected_style(profile, rung, thresholds)
    text = Text()
    text.append("  ")
    text.append(marker, style=chosen_style if selected else _S_MUTED)
    text.append(" ")

    if rung not in thresholds:  # the floor rung: no measurement can disqualify it
        # The dash sits right-aligned in the SCORE column and the threshold
        # column is blanked, so "no measurement applies here" lines up with the
        # numbers above it instead of floating between two columns.
        text.append(f"{rung:<{_RUNG_WIDTH}}", style=chosen_style if selected else _S_MUTED)
        text.append(" ")
        text.append(f"{'-':>{_SCORE_WIDTH}}{' ' * 14}", style=_S_MUTED)
        if selected:
            text.append("SELECTED", style=chosen_style)
            text.append(" - floor (always works)", style=_S_MUTED)
        else:
            # The safety net nobody had to use: present, and deliberately quiet.
            text.append("floor (always works)", style=_S_MUTED)
        return text

    threshold = thresholds[rung]
    need = f"needs {threshold:.2f}"
    # ABSENT from scores means no probe ever ran this rung — which is a different
    # fact from a probe that ran and scored it 0.0 (the runner writes that
    # explicitly when a probe fails). Saying "REJECTED (0.95 short)" for a rung
    # nobody has measured would be the card inventing a measurement, on the one
    # screen whose whole job is telling guesses from evidence. The wording is
    # "not probed" rather than "not measured" because the provenance pins in
    # tests/test_report_card.py forbid the substring "measured" anywhere above
    # the verdict — and this line is exactly the kind of claim they guard.
    score = scores.get(rung)
    if score is None:
        rung_style = chosen_style if selected else _S_MUTED
        text.append(f"{rung:<{_RUNG_WIDTH}}", style=rung_style)
        text.append(" ")
        text.append(f"{'-':>{_SCORE_WIDTH}}", style=_S_MUTED)
        text.append(f"  {need}  ", style=_S_LABEL)
        text.append("SELECTED" if selected else "not probed", style=rung_style)
        return text

    if selected:
        status, rung_style, score_style = "SELECTED", chosen_style, chosen_style
    elif score >= threshold:
        status, rung_style, score_style = "ok, fallback", _S_MUTED, _S_MUTED
    else:
        status = f"REJECTED ({threshold - score:.2f} short)"
        rung_style, score_style = _S_BAD_DIM, _S_BAD_DIM
    text.append(f"{rung:<{_RUNG_WIDTH}}", style=rung_style)
    text.append(" ")
    text.append(f"{score:.2f}", style=score_style)
    # The threshold stays chrome in every row: it is the bar, not the result.
    text.append(f"  {need}  ", style=_S_LABEL)
    text.append(status, style=_S_FALLBACK if status == "ok, fallback" else rung_style)
    if status.startswith("REJECTED"):
        # The status word already carries the red; make the shortfall the bold
        # part of it, since "how far short" is the actionable half.
        text.stylize(_S_BAD, len(text.plain) - len(status), len(text.plain))
    return text


def _is_measured(profile: CapabilityProfile) -> bool:
    """A profile carries real measurements once the deep probe has run: ``source``
    is authoritative, and a stamped ``probed_at`` is honored for hand-built profiles.
    A ``seeded`` profile is provisional (introspected, not measured) even though it,
    too, has ``probed_at is None`` -- ``source`` is what distinguishes the two."""
    return profile.source == "probed" or profile.probed_at is not None


def _source_label(profile: CapabilityProfile) -> str:
    """Honest provenance of the numbers so the user knows guesses from measurements.
    The ``tuned`` branch comes FIRST: a tuned profile is measured-then-adjusted and
    must never be mislabeled plain-measured (or, worse, unprobed)."""
    if profile.source == "tuned":
        return "tuned (measured, then lowered from live-session evidence)"
    if profile.source == "seeded":
        return "seeded (provisional - introspected, measuring in the background)"
    if _is_measured(profile):
        return "measured"
    return "defaults (unprobed)"


def _source_style(profile: CapabilityProfile) -> str:
    """Provenance IS a verdict — about how far the numbers below can be trusted.
    Only a fully measured profile earns the calm green; everything else is
    provisional and says so in the one colour that means "not settled yet"."""
    if _is_measured(profile) and profile.source not in ("tuned", "seeded"):
        return _S_GOOD
    return _S_WARN


def _context_style(profile: CapabilityProfile, honest: int, advertised: int) -> str:
    """Grade the honest/advertised ratio. A model that holds three quarters of
    what it advertises is fine; one that holds a fifth is the reason this tool
    exists, and the card should not make a reader do the division to notice.

    Grading requires a MEASUREMENT. On an unprobed profile both numbers are
    defaults, and on a seeded one the "honest" figure is the endpoint's own
    advertised claim read back — colouring either would be the card inventing a
    finding, on the one screen whose whole job is telling guesses from evidence.
    Those stay chrome and let the Source line speak.
    """
    if not advertised or not _is_measured(profile):
        return _S_MUTED
    ratio = honest / advertised
    if ratio >= _CONTEXT_GOOD:
        return _S_GOOD
    if ratio >= _CONTEXT_FAIR:
        return _S_WARN
    return _S_BAD


def _verdict(profile: CapabilityProfile, proto: str, edit: str) -> str:
    floor = proto == TOOL_PROTOCOL_LADDER[-1] and edit == EDIT_FORMAT_LADDER[-1]
    if profile.source == "tuned":
        # measured base, ladders conservatively lowered by live evidence (MS-8).
        rungs = (
            "floor protocol + whole-file edits" if floor
            else f"{proto} tool calls, {edit} edits"
        )
        return f"tuned - {rungs} (lowered from live-session evidence; run /probe to re-measure)"
    if profile.source == "seeded":
        # provisional-usable: reflects the SEED's ladders, not the floor. A seed with
        # no native signal legitimately lands on the floor rungs -- say so honestly.
        rungs = (
            "floor protocol + whole-file edits" if floor
            else f"{proto} tool calls, {edit} edits"
        )
        return f"seeded (provisional) - {rungs} (introspected, measuring in the background)"
    if _is_measured(profile):
        if floor:
            return "floor-only - text protocol + whole-file edits (usable, slow but safe)"
        return f"usable - {proto} tool calls, {edit} edits"
    return "unprobed - floor-conservative defaults (safe-slow until measured)"


def _verdict_style(profile: CapabilityProfile, proto: str, edit: str) -> str:
    """The bottom line gets the only unqualified green on the card — and only
    when the profile is measured AND cleared something above the floor. Every
    other outcome (floor-only, seeded, tuned, unprobed) is honest-but-provisional
    and takes the warm colour that means "there is more to do here"."""
    floor = proto == TOOL_PROTOCOL_LADDER[-1] and edit == EDIT_FORMAT_LADDER[-1]
    if profile.source in ("tuned", "seeded") or not _is_measured(profile):
        return _S_WARN
    return _S_WARN if floor else _S_GOOD


def render_report_card(profile: CapabilityProfile) -> str:
    """Plain-text capability report for ``/envelope`` (IC-608).

    Exactly :func:`render_report_card_text` with its styling dropped — the two
    can never disagree about a character.
    """
    return render_report_card_text(profile).plain


def render_report_card_text(profile: CapabilityProfile) -> Text:
    """The capability report card, styled (``/envelope``, IC-608).

    A pure function of the profile: model id, honest vs advertised context, the
    recommended tool protocol + edit format with every rung's reliability, retention /
    coherence, and a one-line verdict. Probe notes are NOT shown here — they live on the
    live run, not the persisted profile.

    Colour is applied by MEANING and never alone: the rung that was selected, the
    rung that measured short, the honest-context ratio, and the verdict. Strip
    every span (which :func:`render_report_card` does, and which a non-TTY
    console does for itself) and the card still says all of it in words.
    """
    proto = profile.recommended_tool_protocol()
    edit = profile.recommended_edit_format()
    if profile.probed_at:
        probed = profile.probed_at
    elif profile.source == "seeded":
        probed = "not yet - seeded from introspection, measuring in the background"
    else:
        probed = "never (floor-conservative defaults)"
    honest = profile.honest_context
    advertised = profile.context_window
    pct = f"{100 * honest / advertised:.0f}%" if advertised else "n/a"

    rows: list[Text] = [
        _row("Model:", profile.model_id, _S_HEADING),
        _row("Source:", _source_label(profile), _source_style(profile)),
        # A timestamp is provenance detail, not a finding: it stays chrome.
        _row("Probed:", probed, _S_MUTED),
    ]

    # The thesis line. The measured number is the content; the advertised one is
    # a claim, so it recedes; the ratio between them is graded.
    context = Text()
    context.append(f"{'Context:':<{_LABEL_WIDTH}}", style=_S_LABEL)
    context.append("honest ", style=_S_LABEL)
    context.append(f"{honest:,}", style=_S_HEADING)
    context.append(f" / advertised {advertised:,} tokens ", style=_S_MUTED)
    context.append(f"({pct})", style=_context_style(profile, honest, advertised))
    rows.append(context)

    # "(default)" not "(unmeasured)": the existing provenance pin forbids the
    # substring "measured" before the verdict; the Source line carries provenance.
    ratio = Text()
    ratio.append(f"{'Token ratio:':<{_LABEL_WIDTH}}", style=_S_LABEL)
    ratio.append(f"{profile.chars_per_token:.1f} chars/token", style=_S_VALUE)
    if profile.chars_per_token == 4.0:
        ratio.append("  (default)", style=_S_MUTED)
    rows.append(ratio)

    for label, ladder, scores, thresholds, chosen in (
        ("Tool protocol:", TOOL_PROTOCOL_LADDER, profile.tool_protocols,
         TOOL_PROTOCOL_THRESHOLDS, proto),
        ("Edit format:", EDIT_FORMAT_LADDER, profile.edit_formats,
         EDIT_FORMAT_THRESHOLDS, edit),
    ):
        # The section heading names the rung that won, in the same green the
        # SELECTED row below it carries — heading and evidence tied together.
        head = Text()
        head.append(f"{label:<{_LABEL_WIDTH}}", style=_S_HEADING)
        head.append(chosen, style=_selected_style(profile, chosen, thresholds))
        head.append("  (recommended)", style=_S_MUTED)
        rows += [Text(""), head]
        rows += [_rung_text(profile, rung, scores, thresholds, chosen) for rung in ladder]

    rows += [
        Text(""),
        _row("JSON adherence:", f"{profile.json_adherence:.2f}"),
        _row("Retention:", f"{profile.instruction_retention:.2f}"),
        _row(
            "Coherence:",
            f"{profile.coherence_horizon} turns (anchor every {profile.anchor_cadence()})",
        ),
        # yes/no only (MS-6): the pre-verdict text must never claim "measured".
        _row("Vision:", "yes" if profile.vision else "no"),
        Text(""),
    ]

    verdict = Text()
    verdict.append(f"{'Verdict:':<{_LABEL_WIDTH}}", style=_S_HEADING)
    verdict.append(_verdict(profile, proto, edit), style=_verdict_style(profile, proto, edit))
    rows.append(verdict)

    return Text("\n").join(rows)
