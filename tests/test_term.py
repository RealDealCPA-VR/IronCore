"""The CLI's terminal styling: same palette as the app, and invisible off a TTY.

``ironcore/term.py`` and ``ironcore/tui/theme.py`` hold the same colours on
purpose — ARCHITECTURE.md §4 forbids anything importing ``tui/``, and the CLI
sits below it — so the first test here is the thing that stops the two copies
drifting into two different products.

The rest pin the property every doctor/demo test in the suite silently depends
on: styling is additive. Piped, captured or redirected, what IronCore writes is
character-for-character what ``print`` would have written, with no escape codes
in it.
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

from rich.text import Text

from ironcore import term
from ironcore.tui import theme

ROOT = Path(__file__).resolve().parents[1]

#: Any ANSI escape sequence (CSI or otherwise).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# --------------------------------------------------------------------------- #
# one palette, two consumers
# --------------------------------------------------------------------------- #


def test_cli_palette_matches_the_app_theme():
    """The CLI and the TUI must be the same product. Every colour term.py names
    is the colour theme.py names, or a `doctor` green and a tool-card green stop
    being the same green."""
    for name in ("BACKGROUND", "FOREGROUND", "PRIMARY", "SECONDARY", "ACCENT",
                 "SUCCESS", "WARNING", "ERROR", "MUTED"):
        assert getattr(term, name) == getattr(theme, name), name


def test_risk_and_mode_chips_match_the_app():
    """Risk and autonomy are safety signals; the CLI must not teach a reader a
    different escalation from the one the app draws."""
    assert term.RISK_STYLE == theme.RISK_STYLE
    assert term.MODE_STYLE == theme.MODE_STYLE
    assert term.FILLED_RISKS == theme.FILLED_RISKS
    for risk in ("read", "write", "exec", "net"):
        assert term.risk_chip(risk) == theme.risk_chip(risk)


def test_the_card_rule_is_a_colour_not_a_filled_chip():
    """A card's left rule takes the risk COLOUR. Reusing the chip style would
    lead every WRITE card with a solid block that shouts over the chip."""
    for risk in ("read", "write", "exec", "net"):
        assert "on " not in term.risk_rule_style(risk)


def test_every_risk_and_mode_keeps_its_word():
    """Colour never carries meaning alone (SAFETY): the chip text is the
    accessible carrier and a no-colour terminal must lose nothing."""
    for risk in ("read", "write", "exec", "net"):
        assert risk.upper() in term.risk_chip(risk)
    for mode in ("plan", "manual", "accept-edits", "auto"):
        assert mode in term.mode_chip(mode)


# --------------------------------------------------------------------------- #
# colour is additive: the plain text never changes
# --------------------------------------------------------------------------- #


def test_doctor_line_never_alters_the_text_it_is_given(capsys):
    """Doctor's strings are contract (tests + docs/TROUBLESHOOTING.md quote
    them). `doctor_line` derives its styling FROM the text, so it can only ever
    paint — never reword, pad or realign."""
    lines = [
        "[ok] python 3.11.13 (need >= 3.11)",
        "[--] endpoint not reachable: http://localhost:11434/v1/models",
        "[!!] git not found -- /undo, /redo and change-set snapshots are disabled",
        "[FAIL] config: bad",
        "     start your local server (e.g. `ollama serve`)",
        "no marker at all",
    ]
    for text in lines:
        term.doctor_line(text)
    assert capsys.readouterr().out.splitlines() == lines


def test_nothing_is_wrapped_at_the_console_width(capsys):
    """`soft_wrap` is load-bearing: a doctor line longer than the console would
    otherwise be folded, and every `assert "..." in out` in the suite that spans
    the fold would fail for a reason nobody could see."""
    long_line = "[ok] " + "x" * 400
    term.doctor_line(long_line)
    assert capsys.readouterr().out.splitlines() == [long_line]


def test_capture_is_not_a_terminal_so_no_escape_codes_are_emitted(capsys):
    """The whole suite reads this output as strings. If colour ever leaked into
    a non-tty stream, hundreds of pins would break at once — so pin the cause."""
    term.line(Text("coloured", style=f"bold {term.ERROR}"))
    assert capsys.readouterr().out == "coloured\n"


def test_the_console_follows_the_current_stdout(monkeypatch):
    """The console is cached for the process but must never capture a stream:
    `capsys`, a redirect, or a caller swapping sys.stdout has to be honoured on
    the NEXT write, not the next process."""
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buffer)
    term.line(Text("into the buffer"))
    assert buffer.getvalue() == "into the buffer\n"


# --------------------------------------------------------------------------- #
# glyphs degrade instead of crashing
# --------------------------------------------------------------------------- #


def test_glyphs_fall_back_to_ascii_on_a_stream_that_cannot_encode_them(monkeypatch):
    """`ironcore demo > out.txt` on Windows writes through cp1252, where the
    box-drawing set raises UnicodeEncodeError. Degrade the whole set together —
    a half-ASCII transcript looks broken in a way a plain one does not."""
    monkeypatch.setattr(term.console(), "file", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    assert term.glyphs() is term.ASCII_GLYPHS


def test_the_ascii_fallback_keeps_every_word():
    """The fallback may lose decoration; it may not lose meaning."""
    assert "ok" in term.ASCII_GLYPHS.ok
    assert "error" in term.ASCII_GLYPHS.bad
    assert "ok" in term.UNICODE_GLYPHS.ok  # the word survives the pretty form too
    assert "error" in term.UNICODE_GLYPHS.bad
    assert "".join(vars(term.ASCII_GLYPHS).values()).isascii()
    # and if the "unicode" set were ASCII too, the fallback above would be dead code
    assert not "".join(vars(term.UNICODE_GLYPHS).values()).isascii()


# --------------------------------------------------------------------------- #
# end to end: the shipped commands, really piped
# --------------------------------------------------------------------------- #


def _run(*args: str) -> str:
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-m", "ironcore.cli", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    return proc.stdout


def test_piped_doctor_and_demo_contain_no_ansi_at_all():
    """The end-to-end form of the guarantee above, through the real entry point
    and a real pipe — the case a user hits with `ironcore doctor > report.txt`."""
    for output in (_run("doctor"), _run("demo")):
        assert output.strip()
        assert not _ANSI_RE.search(output), "colour leaked into a non-terminal stream"


def test_piped_demo_is_pure_ascii_on_a_stream_that_cannot_take_more():
    """A redirected Windows console is cp1252. The demo must come out readable
    rather than half-written and then raise mid-transcript."""
    if sys.platform != "win32":  # pragma: no cover — the encoding this guards is Windows-only
        return
    assert _run("demo").isascii()
