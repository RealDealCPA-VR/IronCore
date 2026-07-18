"""The IronCore look: one palette, and the semantic styles built on it.

Design rules this module encodes (they are the reason it exists as a single
file rather than as hex literals sprinkled through the widgets):

1. **One base, one accent.** A cool slate ground — forge iron, not black — with
   exactly one warm colour (ember amber) reserved for attention and for the
   user's own voice. Everything structural is steel blue.
2. **Colour carries meaning, never decoration.** A hue appears only where it
   says something: risk level, gate outcome, diff polarity, autonomy posture.
   Nothing is tinted to look nice.
3. **Quiet by default, loud when it matters.** The common, safe case renders
   calm (a READ chip is plain steel text, MANUAL mode is grey). Elevated risk
   *fills* — a WRITE/EXEC/NET chip and the accept-edits/auto mode chips become
   solid blocks. A wall of read cards therefore makes a single write card jump,
   which is the safety property that matters, not a paint job.
4. **Colour never carries meaning alone** (SAFETY: a colour-blind or
   no-colour terminal must lose nothing). Every chip keeps its word, every
   result keeps its ``ok``/``error`` text, every diff line keeps its ``+``/``-``.

Two consumers, deliberately sharing these constants:

* ``IRONCORE_THEME`` — a ``textual.theme.Theme`` registered by the app, so CSS
  can spend ``$primary`` / ``$accent`` / ``$risk-write`` design tokens.
* ``RISK_STYLE`` / ``MODE_STYLE`` / the ``STYLE_*`` names — Rich style strings
  for the ``Text`` the widgets build by hand.

Rendering only; imports nothing from ``core/`` (docs/ARCHITECTURE.md §4).
"""

from __future__ import annotations

from textual.theme import Theme

# --------------------------------------------------------------------------- #
# the palette
# --------------------------------------------------------------------------- #

#: Ground: a cool slate, one step off black, so panels can lift off it.
BACKGROUND = "#0e1319"
SURFACE = "#161d25"
PANEL = "#1d2630"
FOREGROUND = "#ced7e1"

#: Structure, focus, and calm/read-only states.
PRIMARY = "#6ea8d8"
SECONDARY = "#8bb3cc"
#: The one warm colour: the user's own lines, and anything asking for a human.
ACCENT = "#e3a355"

SUCCESS = "#57c08a"
WARNING = "#e3a355"
ERROR = "#e2606c"

#: Supporting detail: args, timestamps, system notes, the keys hint.
MUTED = "#77879a"

#: The selection band, and its dimmer form for an unfocused list. PRIMARY
#: blended over SURFACE at ~28% / ~15% — pre-blended because Textual's
#: ``block-cursor-*`` tokens take a flat colour, not a colour + alpha.
SELECTION = "#2f4457"
SELECTION_BLURRED = "#233240"

#: Risk design tokens, kept separately from the theme because the app also
#: registers them as ``get_theme_variable_defaults`` — CSS is parsed before any
#: theme is applied, and a token that resolves only inside IRONCORE_THEME would
#: crash startup (and would break a switch to any stock Textual theme).
#: Severity, not category: read is calm, write warns, and the two
#: irreversible-by-default classes share one danger red — the chip's WORD is
#: what tells exec from net.
RISK_VARIABLES: dict[str, str] = {
    "risk-read": PRIMARY,
    "risk-write": WARNING,
    "risk-exec": ERROR,
    "risk-net": ERROR,
}

IRONCORE_THEME = Theme(
    name="ironcore",
    dark=True,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    foreground=FOREGROUND,
    primary=PRIMARY,
    secondary=SECONDARY,
    accent=ACCENT,
    success=SUCCESS,
    warning=WARNING,
    error=ERROR,
    variables={
        "text-muted": MUTED,
        **RISK_VARIABLES,
        # Input/scrollbar chrome, so no widget invents its own grey.
        "border-blurred": PANEL,
        "scrollbar": PANEL,
        "scrollbar-hover": MUTED,
        "scrollbar-active": PRIMARY,
        # Selection (the session picker's row cursor, and any list Textual
        # draws). Set HERE rather than in app CSS because ListView styles its
        # cursor through these tokens from inside a nested `:focus` rule that
        # out-specifies anything the app sheet can say — and because a
        # selection band should look the same everywhere by construction.
        # Pre-blended steel: a flat token cannot carry an alpha.
        "block-cursor-background": SELECTION,
        "block-cursor-foreground": FOREGROUND,
        "block-cursor-text-style": "bold",
        "block-cursor-blurred-background": SELECTION_BLURRED,
        "block-cursor-blurred-foreground": FOREGROUND,
        "block-cursor-blurred-text-style": "bold",
        "block-hover-background": SELECTION_BLURRED,
    },
)

# --------------------------------------------------------------------------- #
# semantic Rich styles
# --------------------------------------------------------------------------- #

#: Filled chip for an elevated-risk class; plain text for the calm one. The
#: keys are ``safety.risk.Risk`` values; an unknown key falls back to MUTED.
RISK_STYLE: dict[str, str] = {
    "read": PRIMARY,
    "write": f"bold {BACKGROUND} on {WARNING}",
    "exec": f"bold {BACKGROUND} on {ERROR}",
    "net": f"bold {BACKGROUND} on {ERROR}",
}

#: Risk classes whose chip is drawn FILLED (and therefore padded with a space
#: on each side). Read stays flat so a routine transcript does not strobe.
FILLED_RISKS = frozenset({"write", "exec", "net"})

#: Autonomy posture, same escalation: the two hands-off modes fill.
MODE_STYLE: dict[str, str] = {
    "plan": f"bold {PRIMARY}",
    "manual": f"bold {MUTED}",
    "accept-edits": f"bold {BACKGROUND} on {WARNING}",
    "auto": f"bold {BACKGROUND} on {ERROR}",
}

#: Tool-card lifecycle. ``awaiting approval`` is the only state that wants a
#: human, so it is the only one that gets the warm accent.
STATE_STYLE: dict[str, str] = {
    "requested": MUTED,
    "awaiting approval": f"bold {ACCENT}",
    # ``done`` is the boring, expected outcome and it is already reported by the
    # green ✓ ok line underneath — colouring it too put three colours on every
    # routine card and left nothing for the states that need one.
    "done": MUTED,
    "error": f"bold {ERROR}",
    "denied": f"bold {ERROR}",
}

STYLE_TOOL_NAME = f"bold {FOREGROUND}"
STYLE_ARGS = MUTED
STYLE_OK = SUCCESS
STYLE_FAIL = f"bold {ERROR}"
STYLE_RESULT = MUTED
STYLE_MUTED = MUTED
STYLE_USER = f"bold {ACCENT}"
STYLE_HEADING = f"bold {FOREGROUND}"


def risk_style(risk: str) -> str:
    """Rich style for a risk chip (unknown risk classes render muted)."""
    return RISK_STYLE.get(risk, MUTED)


def risk_chip(risk: str) -> str:
    """The chip TEXT for ``risk`` — padded when it will be drawn filled, so the
    block has breathing room inside it. The word is always present: this is the
    accessible carrier, and the colour only reinforces it."""
    label = risk.upper()
    return f" {label} " if risk in FILLED_RISKS else label


def mode_style(mode: str) -> str:
    """Rich style for the status bar's mode chip (unknown modes render muted)."""
    return MODE_STYLE.get(mode, f"bold {MUTED}")


def state_style(state: str) -> str:
    """Rich style for a tool card's lifecycle state."""
    return STATE_STYLE.get(state, MUTED)
