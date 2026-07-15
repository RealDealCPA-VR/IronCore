"""Input bar: the command/prompt line with slash-completion (SPEC §3.1, §3.3).

A thin ``Input`` subclass. The only behavior it adds is a Tab binding that
delegates to the app's ``complete`` action so the slash palette (owned by the
app, IC-704) can fill the highlighted command. Enter emits the stock
``Input.Submitted`` message, which the app routes to either a turn or a
command dispatch. History/multiline are declared in SPEC §3.1 and land with
later polish; this file stays a rendering-only leaf.
"""

from __future__ import annotations

from textual.binding import Binding
from textual.widgets import Input


class InputBar(Input):
    """Prompt line. Tab asks the app to complete a partial slash command."""

    BINDINGS = [Binding("tab", "app.complete", "Complete command", show=False)]

    def __init__(self) -> None:
        super().__init__(placeholder="Message, or /command …", id="input")
