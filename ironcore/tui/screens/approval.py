"""Approval modal (IC-703, SAFETY §4).

Shown when the gate returns ``ask``. Renders the request's EXACT effect — the
full diff, the resolved command line, or the URL — never a paraphrase, and
offers three keyboard-first verdicts:

    y  approve once           -> ApprovalAnswer(decision="approve", scope="once")
    n  deny                   -> ApprovalAnswer(decision="deny")
    a  approve all this turn  -> ApprovalAnswer(decision="approve", scope="turn")

The screen resolves purely to an ``ApprovalAnswer`` via ``dismiss``; the app's
push-callback hands that to ``ApprovalBroker.answer``. The screen never touches
the broker or the engine itself — it is a dumb prompt (docs/ARCHITECTURE.md §4).

SAFETY §4 wiring: approving is single-key, but the Approve button is never the
default-focused Enter target for EXEC/NET risk — the Deny button takes initial
focus there, so a reflexive Enter denies. Esc is deliberately unbound: an
approval is a decision, not something to dismiss ambiguously (the running turn
is interrupted with Esc only when no modal is up).
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from ironcore.core.approvals import ApprovalAnswer, ApprovalRequest

#: Risk classes for which Approve must not be the default-focused button.
_HIGH_RISK = frozenset({"exec", "net"})


class ApprovalScreen(ModalScreen[ApprovalAnswer]):
    """Modal that turns a keystroke (y/n/a) into an ``ApprovalAnswer``."""

    BINDINGS = [
        Binding("y", "approve", "Approve", show=True),
        Binding("n", "deny", "Deny", show=True),
        Binding("a", "approve_all", "Approve all (turn)", show=True),
    ]

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        risk = self.request.risk
        with Vertical(id="approval-box"):
            yield Label(Text(f"Approval required — {risk.upper()}"), id="approval-title")
            yield Static(Text(self.request.preview), id="approval-preview")
            with Horizontal(id="approval-buttons"):
                yield Button("Deny (n)", id="deny", variant="error")
                yield Button("Approve (y)", id="approve", variant="success")
                yield Button("Approve all writes (a)", id="approve-all", variant="warning")

    def on_mount(self) -> None:
        # SAFETY §4: EXEC/NET must not default-focus Approve.
        focus_id = "#deny" if self.request.risk in _HIGH_RISK else "#approve"
        self.query_one(focus_id, Button).focus()

    # -- verdicts -------------------------------------------------------------

    def action_approve(self) -> None:
        self.dismiss(ApprovalAnswer(decision="approve", scope="once"))

    def action_deny(self) -> None:
        self.dismiss(ApprovalAnswer(decision="deny"))

    def action_approve_all(self) -> None:
        self.dismiss(ApprovalAnswer(decision="approve", scope="turn"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "deny": self.action_deny,
            "approve": self.action_approve,
            "approve-all": self.action_approve_all,
        }
        handler = actions.get(event.button.id or "")
        if handler is not None:
            handler()
