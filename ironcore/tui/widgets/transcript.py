"""Transcript pane: streaming text, notes, and live tool cards (SPEC §3.1).

A ``VerticalScroll`` of append-only line widgets. Each ``core.events`` event
maps to a mutation here:

* ``TextDelta``      -> append to the *current* assistant bubble in place
  (``_Bubble.append`` mutates one widget's buffer; the whole transcript is
  never reparsed per token — SPEC §3.1 "streaming everywhere").
* ``ToolCallRequested`` -> a ``ToolCard`` mounts immediately, keyed by call id.
* ``ApprovalRequired`` / ``ToolCallFinished`` -> the *same* card updates through
  its states (requested -> awaiting -> done/denied). Nothing happens invisibly
  (SAFETY §1.3).

All dynamic text is wrapped in ``rich.text.Text`` so model / tool output can
never be interpreted as Rich console markup. Every item carries a ``_plain``
mirror of its text so front ends and tests can read the transcript as a string
without walking Rich renderables.

Rendering only — this widget holds no engine reference and mounts nothing from
``core/`` (docs/ARCHITECTURE.md §4).
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from ironcore.providers.base import ToolCall
from ironcore.tools.base import ToolResult
from ironcore.tui.widgets.diffview import diff_to_text, looks_like_diff

#: Max characters of a tool's arg/result preview shown collapsed on the card.
_PREVIEW_CHARS = 100

#: Lines of the edit payload shown colored on a tool card before it's truncated;
#: the full diff lives in the approval modal (scrollable), the card stays compact.
_CARD_DIFF_LINES = 16


def args_preview(arguments: dict) -> str:
    """Compact one-line ``k=v`` preview of tool arguments, truncated."""
    parts = [f"{k}={v!r}" for k, v in arguments.items()]
    text = ", ".join(parts)
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + " …"


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    first = lines[0] if lines else ""
    return first if len(first) <= _PREVIEW_CHARS else first[:_PREVIEW_CHARS] + " …"


def _card_diff_payload(call: ToolCall) -> str | None:
    """The ``edit_file`` diff to colorize on a card, or None for other calls.

    Only an ``edit_file`` whose ``edit`` argument actually looks like a diff (a
    ``unified_diff`` / ``search_replace`` payload) qualifies; a ``whole_file``
    body or a plain ``write_file`` content dump is left to the compact summary
    so cards don't balloon with full file contents.
    """
    if call.name != "edit_file":
        return None
    edit = call.arguments.get("edit")
    if isinstance(edit, str) and edit and looks_like_diff(edit):
        return edit
    return None


class _Bubble(Static):
    """One transcript line/paragraph whose text can grow in place.

    Uses ``render()`` (not ``update()``): ``append`` mutates the buffer and
    refreshes, so streaming touches one widget's text — never a reparse of the
    whole transcript (SPEC §3.1).
    """

    def __init__(self, text: str, *, classes: str) -> None:
        self._plain = text
        super().__init__(classes=classes)

    def render(self) -> RenderableType:
        return Text(self._plain)

    def append(self, text: str) -> None:
        self._plain += text
        self.refresh()


class ToolCard(Static):
    """A tool call rendered the moment it is requested, updated through states.

    States: ``requested`` (gate decision known) -> ``awaiting approval`` (an
    ask is pending) -> ``done`` / ``error`` (result) or ``denied`` (user/policy).
    """

    def __init__(self, call: ToolCall, risk: str, decision: str) -> None:
        self.call = call
        self.risk = risk
        self.decision = decision
        self.state = "requested"
        self.result: ToolResult | None = None
        self.note: str | None = None
        #: An ``edit_file`` diff payload to colorize under the card, or None.
        self._diff = _card_diff_payload(call)
        self._plain = ""
        super().__init__(classes="tool-card")
        self._plain = self._build()

    def render(self) -> RenderableType:
        base = Text(self._plain)
        if self._diff is None:
            return base
        # The plain header/summary (mirrored in _plain for tests) plus the
        # colored diff — SPEC §3.1 "tool cards ... diff views".
        return Group(base, diff_to_text(self._diff, max_lines=_CARD_DIFF_LINES))

    def set_state(self, state: str) -> None:
        self.state = state
        self._refresh()

    def set_finished(self, result: ToolResult) -> None:
        self.result = result
        self.state = "done" if result.ok else "error"
        self._refresh()

    def set_denied(self, reason: str | None) -> None:
        self.state = "denied"
        self.note = reason
        self._refresh()

    def _refresh(self) -> None:
        self._plain = self._build()
        self.refresh()

    def _build(self) -> str:
        header = f"▸ {self.call.name}  [{self.risk}]  {self.state}"
        lines = [header]
        # When the diff is shown colored below, drop its raw payload from the
        # one-line arg preview so it isn't printed twice.
        display_args = self.call.arguments
        if self._diff is not None:
            display_args = {k: v for k, v in display_args.items() if k != "edit"}
        preview = args_preview(display_args)
        if preview:
            lines.append(f"    {preview}")
        if self.result is not None:
            mark = "✓ ok" if self.result.ok else "✗ error"
            body = _first_line(self.result.output or self.result.error or "")
            lines.append(f"    {mark}  {body}")
        elif self.note:
            lines.append(f"    ✗ {_first_line(self.note)}")
        return "\n".join(lines)


class Transcript(VerticalScroll):
    """The scrolling conversation view. All mutators that mount are async so
    the driving worker can await ordered insertion."""

    def __init__(self) -> None:
        super().__init__(id="transcript")
        self._items: list[Static] = []
        self._current: _Bubble | None = None
        self._cards: dict[str, ToolCard] = {}

    async def _add(self, item: Static) -> None:
        self._items.append(item)
        await self.mount(item)
        self.scroll_end(animate=False)

    async def add_user(self, text: str) -> None:
        self._current = None
        await self._add(_Bubble(f"› {text}", classes="user"))

    async def append_assistant(self, text: str) -> None:
        if self._current is None:
            self._current = _Bubble("", classes="assistant")
            await self._add(self._current)
        self._current.append(text)
        self.scroll_end(animate=False)

    def end_assistant(self) -> None:
        """Close the current assistant bubble; the next delta starts a new one."""
        self._current = None

    async def add_note(self, text: str) -> None:
        """A dim system line: mode changes, command output, errors, interrupts."""
        self._current = None
        await self._add(_Bubble(text, classes="note"))

    async def add_card(self, call: ToolCall, risk: str, decision: str) -> ToolCard:
        self._current = None
        card = ToolCard(call, risk, decision)
        self._cards[call.id] = card
        await self._add(card)
        return card

    def card(self, call_id: str) -> ToolCard | None:
        return self._cards.get(call_id)

    def plain_text(self) -> str:
        """The whole transcript as plain text — the read surface for tests."""
        return "\n".join(getattr(item, "_plain", "") for item in self._items)
