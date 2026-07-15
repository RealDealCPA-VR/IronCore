"""Textual TUI — phase 7 (IC-701..IC-706).

Layout (SPEC.md #3): transcript pane with streaming markdown and tool
cards, input bar with slash-command completion, status bar with mode chip
(Shift+Tab cycles safety.modes.CYCLE), model name, and token meter.
Approval prompts render as modal dialogs fed by core.events.ApprovalRequired.

The TUI is a THIN client: it renders core.events and answers approval
futures. All logic lives below core/ — if a feature needs an import from
tui/ into core/, the design is wrong (docs/ARCHITECTURE.md #4).
"""
