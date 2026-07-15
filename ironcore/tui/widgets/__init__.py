"""TUI widgets: the three transcript/input/status regions (SPEC §3.1).

Each widget renders only — it holds no engine reference and imports nothing
from ``core/`` (docs/ARCHITECTURE.md §4). The app owns all state and pushes it
into these leaves.
"""

from ironcore.tui.widgets.inputbar import InputBar
from ironcore.tui.widgets.statusbar import StatusBar
from ironcore.tui.widgets.transcript import ToolCard, Transcript

__all__ = ["InputBar", "StatusBar", "ToolCard", "Transcript"]
