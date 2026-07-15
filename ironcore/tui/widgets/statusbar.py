"""Status bar: mode chip + model name + a token/turn meter (SPEC §3.1).

    ``[MANUAL] qwen3-coder:30b · turn 3 · 1.2k tok``

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
        self._running = False
        self._plain = ""  # mirror of the rendered line (read surface for tests)
        super().__init__(id="status")
        self._plain = self._build()

    def render(self) -> RenderableType:
        return Text(self._plain)

    def set_mode(self, mode: Mode) -> None:
        self._mode = mode
        self._refresh()

    def set_running(self, running: bool) -> None:
        self._running = running
        self._refresh()

    def record_turn(self, usage: dict[str, int]) -> None:
        """One completed turn: bump the counter, accumulate token spend."""
        self._turn += 1
        self._tokens += int(usage.get("total_tokens", 0))
        self._refresh()

    def _refresh(self) -> None:
        self._plain = self._build()
        self.refresh()

    def _build(self) -> str:
        chip = f"[{self._mode.value.upper()}]"
        meter = f"turn {self._turn} · {_humanize(self._tokens)} tok"
        parts = [chip, self._model, meter]
        if self._running:
            parts.append("working…")
        return "  ·  ".join(parts)
