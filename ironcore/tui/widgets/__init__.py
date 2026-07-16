"""TUI widgets: the three transcript/input/status regions (SPEC §3.1).

Each widget renders only — it holds no engine reference and imports nothing
from ``core/`` (docs/ARCHITECTURE.md §4). The app owns all state and pushes it
into these leaves.
"""

from ironcore.tui.widgets.diffview import DiffView, diff_to_text, looks_like_diff
from ironcore.tui.widgets.inputbar import InputBar
from ironcore.tui.widgets.statusbar import StatusBar
from ironcore.tui.widgets.transcript import ToolCard, Transcript
from ironcore.tui.widgets.workflowview import WorkflowView, render_progress

__all__ = [
    "DiffView",
    "InputBar",
    "StatusBar",
    "ToolCard",
    "Transcript",
    "WorkflowView",
    "diff_to_text",
    "looks_like_diff",
    "render_progress",
]
