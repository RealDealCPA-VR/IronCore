"""The IronCore look for everything printed OUTSIDE the Textual app.

``ironcore demo``, ``ironcore doctor``, ``ironcore init`` and the banner write
to stdout; they are not Textual widgets, so they cannot spend the app's design
tokens. This module gives them the same palette and the same rules the TUI
theme states (``ironcore/tui/theme.py``):

1. **One base, one accent.** A cool slate ground with exactly one warm colour
   (ember amber) reserved for attention and for the user's own voice.
2. **Colour carries meaning, never decoration.** A hue appears only where it
   says something: risk class, gate outcome, diff polarity, autonomy posture,
   pass/fail.
3. **Quiet by default, loud when it matters.** A READ chip is flat steel and an
   allowed gate is grey; a WRITE/EXEC/NET chip *fills*, and a gate that asked or
   denied takes a colour. A wall of routine lines therefore makes the one line
   that matters jump.
4. **Colour never carries meaning alone.** Every marker keeps its word
   (``[ok]``, ``[FAIL]``, ``READ``), every diff line keeps its ``+``/``-``, so a
   no-colour terminal or a colour-blind reader loses nothing.

Three properties this module must never lose:

* **Colour disappears when stdout is not a terminal.** Rich decides that from
  ``sys.stdout.isatty()`` on every write, and :func:`line` prints with
  ``soft_wrap=True`` (no wrapping, no cropping, no padding), so a piped or
  captured run emits exactly the characters ``print`` would have. The suite
  reads that plain text through ``capsys`` and pins it verbatim.
* **No glyph is printed that the stream cannot encode.** A Windows console
  redirected to a file is cp1252, where ``─`` and ``✓`` raise
  ``UnicodeEncodeError`` — so :func:`glyphs` degrades the whole set to ASCII
  together rather than crashing (or printing a half-ASCII transcript).
* **The console is resolved per write, never captured.** ``Console(file=None)``
  reads ``sys.stdout`` at write time, so a cached console still follows
  ``capsys`` and still re-decides ``is_terminal``.

The palette is duplicated from ``tui/theme.py`` deliberately: ARCHITECTURE.md §4
forbids anything importing ``tui/``, and the CLI sits below it.
``tests/test_term.py`` pins the two copies together so they cannot drift.

Leaf module: imports rich and the stdlib, nothing from ``ironcore``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from rich.console import Console
from rich.text import Text

# --------------------------------------------------------------------------- #
# the palette (mirrors ironcore/tui/theme.py — see the module docstring)
# --------------------------------------------------------------------------- #

BACKGROUND = "#0e1319"
FOREGROUND = "#ced7e1"

PRIMARY = "#6ea8d8"
SECONDARY = "#8bb3cc"
#: The one warm colour: the user's own lines, and anything asking for a human.
ACCENT = "#e3a355"

SUCCESS = "#57c08a"
WARNING = "#e3a355"
ERROR = "#e2606c"

#: Supporting detail: args, labels, follow-up advice, system notes.
MUTED = "#77879a"

#: Structural rules. A mid steel that stays visible on a terminal background of
#: any dark shade — the TUI's ``$panel`` would vanish on anything lighter than
#: its own ``$background``, and a rule nobody can see is furniture, not structure.
DIVIDER = "#46566a"

# --------------------------------------------------------------------------- #
# semantic styles
# --------------------------------------------------------------------------- #

#: Body text is deliberately UNSTYLED: it inherits the terminal's own
#: foreground, so IronCore never fights a user's colour scheme for the 90% of
#: characters that carry no semantics. Only meaning gets a colour.
STYLE_BODY = ""
STYLE_HEADING = "bold"
STYLE_LABEL = MUTED
STYLE_MUTED = MUTED
STYLE_RULE = DIVIDER

STYLE_OK = SUCCESS
STYLE_FAIL = f"bold {ERROR}"
STYLE_WARN = f"bold {WARNING}"
STYLE_NOTE = WARNING
STYLE_USER = f"bold {ACCENT}"
STYLE_TOOL_NAME = "bold"

#: Diff polarity. Removed lines read as loss, added lines as gain.
STYLE_DIFF_MINUS = ERROR
STYLE_DIFF_PLUS = SUCCESS

#: Risk classes: severity, not category. Read is calm; the classes that can
#: change or leave the machine fill, and the chip's WORD tells exec from net.
RISK_STYLE: dict[str, str] = {
    "read": PRIMARY,
    "write": f"bold {BACKGROUND} on {WARNING}",
    "exec": f"bold {BACKGROUND} on {ERROR}",
    "net": f"bold {BACKGROUND} on {ERROR}",
}
FILLED_RISKS = frozenset({"write", "exec", "net"})

#: The same severity as a flat FOREGROUND colour, for the accent rule drawn down
#: a tool card's left edge. The chip fills; the rule must not, or a WRITE card
#: would be led by a two-cell block of solid amber that shouts louder than the
#: chip it is supposed to support (the TUI draws this as a `border-left`, which
#: is a coloured line, not a filled chip).
RISK_RULE: dict[str, str] = {
    "read": PRIMARY,
    "write": WARNING,
    "exec": ERROR,
    "net": ERROR,
}

#: Autonomy posture, same escalation: the two hands-off modes fill.
MODE_STYLE: dict[str, str] = {
    "plan": f"bold {PRIMARY}",
    "manual": f"bold {MUTED}",
    "accept-edits": f"bold {BACKGROUND} on {WARNING}",
    "auto": f"bold {BACKGROUND} on {ERROR}",
}

#: Gate outcomes. ``allow`` is the routine case and stays grey on purpose: if
#: every allowed call were green there would be no colour left for the call that
#: stopped for a human, which is the one a reader must not miss.
GATE_STYLE: dict[str, str] = {
    "allow": MUTED,
    "ask": f"bold {ACCENT}",
    "deny": f"bold {ERROR}",
}

#: Doctor's marker column. The words are contract (tests and TROUBLESHOOTING.md
#: quote them); this only decides their colour.
#: Four ranks, four hues, so the column can be read down at a glance: a check
#: that passed, a fact worth knowing, something to look at, something to fix.
#: ``[--]`` takes the calm steel rather than grey — it is not a problem (doctor
#: still exits 0) but it IS the line a stranger acts on most often ("no config
#: file", "endpoint not reachable"), and grey-on-grey hid it completely.
MARKER_STYLE: dict[str, str] = {
    "[ok]": SUCCESS,
    "[--]": SECONDARY,
    "[!!]": STYLE_WARN,
    "[FAIL]": STYLE_FAIL,
}


def risk_style(risk: str) -> str:
    """Rich style for a risk chip (unknown risk classes render muted)."""
    return RISK_STYLE.get(risk, MUTED)


def risk_chip(risk: str) -> str:
    """The chip TEXT for ``risk`` — padded when it will be drawn filled, so the
    block has breathing room inside it. The word is always present: it is the
    accessible carrier, and the colour only reinforces it."""
    label = risk.upper()
    return f" {label} " if risk in FILLED_RISKS else label


def risk_rule_style(risk: str) -> str:
    """Rich style for a tool card's left accent rule (never a filled chip)."""
    return RISK_RULE.get(risk, MUTED)


def gate_style(decision: str) -> str:
    """Rich style for a gate decision (unknown decisions render muted)."""
    return GATE_STYLE.get(decision, MUTED)


def mode_style(mode: str) -> str:
    """Rich style for an autonomy-mode chip (unknown modes render muted)."""
    return MODE_STYLE.get(mode, f"bold {MUTED}")


def mode_chip(mode: str) -> str:
    """The chip TEXT for ``mode`` — padded when it will be drawn filled."""
    return f" {mode} " if mode in ("accept-edits", "auto") else mode


# --------------------------------------------------------------------------- #
# glyphs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Glyphs:
    """The drawing set for one output stream — Unicode, or an ASCII stand-in.

    Chosen as a SET: a stream that cannot encode one of these cannot encode any
    of them, and a transcript that mixes ``─`` with ``-`` looks broken in a way
    that a consistently ASCII one does not.
    """

    rule: str
    bar: str
    ok: str
    bad: str
    user: str
    dot: str


UNICODE_GLYPHS = Glyphs(rule="─", bar="▏", ok="✓ ok", bad="✗ error", user="›", dot="·")
#: The stand-in for a cp1252 console (or any stream that cannot take the above).
#: Every mark keeps its WORD, so the fallback loses decoration and no meaning.
ASCII_GLYPHS = Glyphs(rule="-", bar="|", ok="[ok]", bad="[error]", user=">", dot="-")


@lru_cache(maxsize=1)
def _cached_console() -> Console:
    """One console for the process. ``file=None`` means it resolves ``sys.stdout``
    on every write, so this survives ``capsys`` and re-decides ``is_terminal``.

    ``legacy_windows`` is forced off under ``FORCE_COLOR``. Rich's Windows
    autodetection asks the *handle* whether it speaks VT; a pipe does not, so it
    falls back to the Win32 colour API — which paints a real console correctly
    but writes nothing at all into a redirect. Someone who set FORCE_COLOR asked
    for escape codes in the stream, so give them escape codes. (This is the same
    truecolor output a modern Windows Terminal gets natively, which is what lets
    the screenshot generator capture a piped run and still show what a user
    sees.) ``markup=False`` is safety, not style: tool output and file paths must
    never be reinterpreted as Rich markup.
    """
    forced = bool(os.environ.get("FORCE_COLOR"))
    return Console(
        soft_wrap=True,
        highlight=False,
        emoji=False,
        markup=False,
        legacy_windows=False if forced else None,
    )


def console() -> Console:
    """The shared stdout console (colour auto-disables when not a terminal)."""
    return _cached_console()


def glyphs() -> Glyphs:
    """The drawing set stdout can actually encode.

    Re-checked per call rather than cached: the encoding belongs to the stream,
    and the stream is resolved per write (``capsys``, a redirect in-process).
    """
    encoding = getattr(console().file, "encoding", None) or "ascii"
    probe = "".join((UNICODE_GLYPHS.rule, UNICODE_GLYPHS.bar, UNICODE_GLYPHS.ok,
                     UNICODE_GLYPHS.bad, UNICODE_GLYPHS.user, UNICODE_GLYPHS.dot))
    try:
        probe.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return ASCII_GLYPHS
    return UNICODE_GLYPHS


def width() -> int:
    """Columns available for full-width structure (rules)."""
    return console().width


# --------------------------------------------------------------------------- #
# printing
# --------------------------------------------------------------------------- #


def line(text: Text | str = "") -> None:
    """Print one line.

    ``soft_wrap=True`` is the load-bearing argument: it turns off wrapping,
    cropping and right-padding, so what reaches a pipe is character-for-character
    what ``print`` would have written. Everything else here is colour, and Rich
    drops colour when stdout is not a terminal.
    """
    console().print(text, soft_wrap=True)


def rule(style: str = STYLE_RULE) -> Text:
    """A full-width horizontal rule in the current glyph set."""
    return Text(glyphs().rule * width(), style=style)


#: Every builder here composes with ``append``: a style passed to ``Text(...)``
#: is the object's BASE style and would bleed into everything appended after it.


def heading(title: str, subtitle: str = "") -> Text:
    """A section title: the name asserts, the gloss recedes."""
    text = Text()
    text.append(title, style=f"bold {ACCENT}")
    if subtitle:
        text.append(f"  {subtitle}", style=STYLE_MUTED)
    return text


def field(label: str, value: Text | str, *, label_width: int = 10) -> Text:
    """One ``label   value`` row with the labels in a fixed, muted column."""
    text = Text()
    text.append(f"{label:<{label_width}} ", style=STYLE_LABEL)
    text.append(value if isinstance(value, Text) else Text(value))
    return text


#: Doctor's marker words, longest first so ``[--]`` cannot shadow a longer one.
_MARKERS = ("[FAIL]", "[ok]", "[--]", "[!!]")


def doctor_line(text: str) -> None:
    """Print one ``doctor`` line, coloured by the marker it already carries.

    Styling is *derived from the text*, never composed beside it, which is why
    doctor's own strings stay exactly as they were: this function cannot change
    what is printed, only how it is painted. Three ranks:

    * the marker word — the severity, in its colour;
    * the finding itself — the terminal's own foreground (bold when it FAILED,
      because that is the line the reader came for);
    * an indented follow-up — muted, so the advice reads as attached to the
      finding above it rather than as another finding.
    """
    for marker in _MARKERS:
        if text.startswith(marker):
            # Built by append, never ``Text(str, style=...)``: a constructor style
            # is the BASE style of the whole object, so the finding would inherit
            # the marker's colour and every routine line would read as decorated.
            out = Text()
            out.append(marker, style=MARKER_STYLE[marker])
            out.append(text[len(marker):], style="bold" if marker == "[FAIL]" else STYLE_BODY)
            line(out)
            return
    if text.startswith(" "):  # an indented follow-up to the line above it
        line(Text(text, style=STYLE_MUTED))
        return
    line(Text(text))
