"""Status bar: mode chip + model name + token/turn meter + key hints (SPEC §3.1).

    ``[MANUAL] · qwen3-coder:30b · turn 3 · 1.2k tok · shift+tab mode · esc stop …``

The trailing key hint is deliberate and permanent: the app's BINDINGS are
otherwise announced only in a mount note that scrolls out of the transcript,
which leaves a stranger in a full-screen app with no way to learn how to quit.

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


def _humanize(n: int) -> str:
    """Compact token count: 950 -> '950', 1234 -> '1.2k'."""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k"


class StatusBar(Static):
    """One-line status: mode, model, turn counter, cumulative tokens.

    Uses the ``render()`` override (not ``update()``): the app mutates state
    then the bar recomputes ``_plain`` and refreshes.
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
        self._plain = self._build()

    def render(self) -> RenderableType:
        return Text(self._plain)

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
        self._plain = self._build()
        self.refresh()

    @staticmethod
    def keys_hint() -> str:
        """The app's BINDINGS in one line. Persistent discovery: the mount note
        scrolls out of the transcript, so the only durable place a stranger can
        learn how to LEAVE a full-screen app is the bar itself."""
        return "shift+tab mode · esc stop · ctrl+c quit · / commands"

    def _build(self) -> str:
        chip = f"[{self._mode.value.upper()}]"
        meter = f"turn {self._turn} · {_humanize(self._tokens)} tok"
        parts = [chip, self._model, meter]
        if self._busy:
            parts.append("working…")
        parts.append(self.keys_hint())
        return "  ·  ".join(parts)
