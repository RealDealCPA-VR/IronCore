"""TUI screens: modal overlays over the main app.

Currently the approval modal (IC-703). The diff viewer (IC-705) and the
session picker (IC-706) land here as sibling screens. Screens are dumb: they
render a request and resolve to a plain value via ``dismiss`` — no screen holds
an engine or broker reference (docs/ARCHITECTURE.md §4).
"""

from ironcore.tui.screens.approval import ApprovalScreen

__all__ = ["ApprovalScreen"]
