"""Status bar: mode chip + model name + token/turn meter + key hints (SPEC §3.1).

    ``[MANUAL] · qwen3-coder:30b · turn 3 · 1.2k tok      shift+tab mode · …``

The key hint is deliberate and permanent: the app's BINDINGS are otherwise
announced only in a mount note that scrolls out of the transcript, which leaves
a stranger in a full-screen app with no way to learn how to quit.

"Permanent" has to survive a narrow terminal, and the bar is a single CSS row
(``height: 1``) so an over-long line is CLIPPED, not wrapped. A hint simply
appended on the left-flowing line is therefore the one variant that disappears
exactly when it matters: at 80 columns with the default model, ``ctrl+c quit``
fell off the end, and during a turn (the line grows by ``working…``) ``esc
stop`` went with it. So the hint is RIGHT-ALIGNED to the last column and
degrades through ``_KEY_TIERS`` — every tier keeps ``ctrl+c``, and the model
name (the one compressible part) is ellipsized before any key is dropped.

The bar is a passive renderer: the app pushes state in via ``set_mode`` /
``record_turn`` / ``set_running`` and the bar recomputes its one line. It
holds no engine reference — nothing in ``tui/`` reaches back into ``core/``
(docs/ARCHITECTURE.md §4); the app is the only thing that mutates it.
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text
from textual.widgets import Static

from ironcore.safety.modes import Mode
from ironcore.tui import theme

#: Key hints, widest first. Narrower terminals fall down the ladder; every rung
#: keeps ``ctrl+c quit``, because "how do I leave this thing" is the one hint a
#: stranger cannot recover on their own.
_KEY_TIERS: tuple[str, ...] = (
    "shift+tab mode · esc stop · ctrl+c quit · / commands",
    "shift+tab · esc stop · ctrl+c quit · /",
    "esc stop · ctrl+c quit",
    "ctrl+c quit",
)

#: mid-turn the ladder stops one rung early, so ``esc stop`` survives with
#: ``ctrl+c quit``: the interrupt key matters most exactly when the line is at
#: its longest (``working…`` is on it) and a stranger wants out of the TURN,
#: not out of the app. Below that rung the model name gets ellipsized instead.
_BUSY_TIERS = _KEY_TIERS[:3]
_FLOOR_HINT = _KEY_TIERS[-1]

#: minimum blank columns between the state and the right-aligned hint.
_GAP = 2

_SEP = "  ·  "

#: The signature ember tick that opens the bar — the same left-bar the masthead,
#: the tool cards, and the slash palette all lead with, so the status row reads
#: as one more panel of the same forged instrument rather than a detached strip.
_TICK = "▍ "

#: The in-flight marker, matched back out of the fitted line to accent it.
_BUSY = "working…"


def _humanize(n: int) -> str:
    """Compact token count: 950 -> '950', 1234 -> '1.2k'."""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k"


class StatusBar(Static):
    """One-line status: mode, model, turn counter, cumulative tokens, keys.

    Uses the ``render()`` override (not ``update()``): the app mutates state
    then the bar recomputes ``_plain`` and refreshes. ``_plain`` mirrors what is
    actually drawn at the current width — including the truncation — so a test
    that reads it is measuring the real row, not an unconstrained ideal.
    """

    def __init__(self, *, mode: Mode, model: str) -> None:
        self._mode = mode
        self._model = model
        self._turn = 0
        self._tokens = 0
        #: a turn is in flight. NOT ``_running``: ``MessagePump.__init__`` owns
        #: that name (``is_running`` returns it, and the pump sets it True once
        #: the widget's message loop starts). Reusing it painted a spurious
        #: "working…" on every idle re-render AND made ``set_running(False)``
        #: report a live widget as stopped, which gates ``check_idle``.
        self._busy = False
        self._plain = ""  # mirror of the rendered line (read surface for tests)
        super().__init__(id="status")
        self._plain = self._fit(self._width())

    def render(self) -> RenderableType:
        # Width is only known once the compositor has placed us, and it changes
        # on every terminal resize — so the line is fitted here, at draw time.
        self._plain = self._fit(self._width())
        return self._stylize(self._plain)

    def _stylize(self, line: str) -> Text:
        """Paint the fitted line: mode chip by autonomy, model name in the
        foreground, everything else supporting detail.

        Styling is applied to the ALREADY-FITTED string by offset, so a line
        that ``_fit`` truncated or ellipsized is coloured exactly as far as it
        actually goes — there is no second, unconstrained render to disagree
        with ``_plain``.
        """
        # Base: the meter, the separators and the keys hint are all supporting
        # detail. The three things worth reading get lifted back out below.
        text = Text(line, style=theme.STYLE_MUTED, no_wrap=True)
        # The ember tick is furniture, not data — style the bar itself, never
        # its trailing space, so a single warm cell opens the row.
        if line.startswith(_TICK):
            text.stylize(f"bold {theme.ACCENT}", 0, 1)
        # The chip is located (not assumed at index 0): the tick precedes it, and
        # an ellipsized state may have shifted or dropped it entirely.
        chip = f"[{self._mode.value.upper()}]"
        idx = line.find(chip)
        if idx == -1:  # squeezed past the chip: nothing more to paint
            return text
        # The autonomy posture is the one thing that must never be missed: PLAN
        # and MANUAL stay flat, accept-edits and auto fill (theme.MODE_STYLE).
        text.stylize(theme.mode_style(self._mode.value), idx, idx + len(chip))
        start = idx + len(chip) + len(_SEP)
        if line[idx + len(chip) :].startswith(_SEP):
            end = line.find(_SEP, start)
            text.stylize(theme.FOREGROUND, start, end if end != -1 else len(line))
        busy = line.find(_BUSY)
        if busy != -1:
            text.stylize(theme.ACCENT, busy, busy + len(_BUSY))
        return text

    def on_resize(self) -> None:
        self._refresh()

    def set_mode(self, mode: Mode) -> None:
        self._mode = mode
        self._refresh()

    def set_model(self, model: str) -> None:
        """Live model swaps (MS-2): the app pushes the new name after /model."""
        self._model = model
        self._refresh()

    def set_running(self, running: bool) -> None:
        self._busy = running
        self._refresh()

    def record_turn(self, usage: dict[str, int]) -> None:
        """One completed turn: bump the counter, accumulate token spend."""
        self._turn += 1
        self._tokens += int(usage.get("total_tokens", 0))
        self._refresh()

    def _refresh(self) -> None:
        self._plain = self._fit(self._width())
        self.refresh()

    # -- rendering ------------------------------------------------------------

    def _width(self) -> int:
        """Content columns available to the line; 0 before the widget is placed
        (unit tests build a bare bar), which means "render unconstrained"."""
        return self.size.width

    @staticmethod
    def keys_hint() -> str:
        """The app's BINDINGS in one line — the widest tier. Persistent
        discovery: the mount note scrolls out of the transcript, so the only
        durable place a stranger can learn how to LEAVE a full-screen app is
        the bar itself. What actually fits is chosen by ``_fit``."""
        return _KEY_TIERS[0]

    def _state(self, model: str) -> str:
        """The left half: the ember tick, mode chip, model, meter, in-flight mark."""
        parts = [f"[{self._mode.value.upper()}]", model]
        parts.append(f"turn {self._turn} · {_humanize(self._tokens)} tok")
        if self._busy:
            parts.append(_BUSY)
        return _TICK + _SEP.join(parts)

    def _fit(self, width: int) -> str:
        """The state on the left, the widest hint that fits on the right.

        Ordering of sacrifices, most expendable first: hint detail, then the
        model name, then the rest of the state. The quit key is never dropped.
        """
        state = self._state(self._model)
        if width <= 0:  # unplaced widget: no constraint to fit to
            return f"{state}{_SEP}{self.keys_hint()}"
        tiers = _BUSY_TIERS if self._busy else _KEY_TIERS
        for hint in tiers:
            if len(state) + _GAP + len(hint) <= width:
                return state + " " * (width - len(state) - len(hint)) + hint

        # Nothing fits beside the full state, so squeeze the state down to the
        # floor hint instead of dropping keys any further.
        hint = tiers[-1]
        if width <= len(hint) + _GAP:
            hint = _FLOOR_HINT
        if width <= len(hint) + _GAP:  # absurdly narrow: the quit key wins outright
            return hint[:width]
        budget = width - len(hint) - _GAP
        overflow = len(state) - budget
        model = self._model
        if 0 < overflow < len(model):
            model = model[: len(model) - overflow - 1] + "…"
        state = self._state(model)
        if len(state) > budget:  # even a one-char model does not fit
            state = state[: budget - 1] + "…"
        return state + " " * (width - len(state) - len(hint)) + hint
