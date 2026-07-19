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
from textual.widget import Widget
from textual.widgets import Button, Label, Static

from ironcore.core.approvals import ApprovalAnswer, ApprovalRequest
from ironcore.tui import theme
from ironcore.tui.widgets.diffview import DiffView, clean_edit_payload, looks_like_diff

#: Risk classes for which Approve must not be the default-focused button.
_HIGH_RISK = frozenset({"exec", "net"})

def _presentable(preview: str) -> str:
    """The engine's exact-effect preview, with an edit body made legible.

    The edit_file preview is ``"edit_file <path> [<fmt>]\\n<payload>"``; this
    keeps that heading verbatim (it says which file and format are at stake) and
    only rewrites a SEARCH/REPLACE payload beneath it into ``-``/``+`` lines. A
    write_file line, a shell command, a unified diff, or a URL has no such body
    (or does not parse as one) and passes through untouched — the transform is
    total and never invents an effect the engine did not describe.
    """
    head, sep, body = preview.partition("\n")
    if not sep:
        return preview
    return f"{head}\n{clean_edit_payload(body)}"


#: What each risk class means, in the words a stranger needs at the moment of
#: deciding. The gate's own class names (SAFETY §2) are jargon on their own.
_CONSEQUENCE: dict[str, str] = {
    "read": "this reads a file outside the workspace",
    "write": "this changes files in your workspace",
    "exec": "this runs a command on your machine",
    "net": "this sends a request over the network",
}


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
        box = Vertical(id="approval-box", classes=f"risk-{risk}")
        # The border carries the colour; the chip inside carries the word. The
        # title deliberately does NOT repeat the risk class — saying "WRITE"
        # in the frame AND in the chip one line below just read as a stutter.
        box.border_title = "Approval required"
        # The verdict keys ride the frame's foot, so the buttons below can stay
        # flat: the decision is always one keystroke away and the modal says so.
        box.border_subtitle = "y approve · n deny · a all"
        with box:
            yield Label(self._title_text(), id="approval-title")
            yield self._preview_widget()
            with Horizontal(id="approval-buttons"):
                yield Button("Deny (n)", id="deny")
                yield Button("Approve (y)", id="approve")
                yield Button("Approve all writes (a)", id="approve-all")

    def _title_text(self) -> Text:
        """The risk chip plus what that class actually MEANS, in plain words.

        Deliberately quiet — the diff below is the hero of this screen — but
        not empty: "WRITE" tells a stranger nothing on its own, and this is the
        one moment where the consequence has to be legible before they choose.
        The chip repeats the border colour in text, so a terminal that drops
        border titles (or all colour) still shows the risk class.
        """
        risk = self.request.risk
        text = Text()
        because = _CONSEQUENCE.get(risk, "this needs your approval")
        text.append(theme.risk_chip(risk), style=theme.risk_style(risk))
        text.append(f"  {because}", style=theme.STYLE_MUTED)
        return text

    def _preview_widget(self) -> Widget:
        """The exact-effect preview: a colored diff for write/edit requests,
        plain text otherwise (a shell ``$ …`` line, a URL, an out-of-jail read).

        A preview whose body does not look like a diff still falls back to plain
        text, so an unexpected shape renders honestly instead of as noise.
        """
        preview = _presentable(self.request.preview)
        if self.request.risk == "write" or looks_like_diff(preview):
            return DiffView(preview, id="approval-preview")
        return Static(Text(preview), id="approval-preview")

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
