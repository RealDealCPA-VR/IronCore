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

import re

from rich.console import Group, RenderableType
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from ironcore.providers.base import ToolCall
from ironcore.tools.base import ToolResult
from ironcore.tui import theme
from ironcore.tui.widgets.diffview import diff_to_text, looks_like_diff

#: Max characters of a tool's arg/result preview shown collapsed on the card.
_PREVIEW_CHARS = 100

#: Lines of the edit payload shown colored on a tool card before it's truncated;
#: the full diff lives in the approval modal (scrollable), the card stays compact.
_CARD_DIFF_LINES = 16

#: Indent for a card's supporting lines (args, result) under its header. Four
#: columns read as a hanging indent without pushing the result off a narrow
#: terminal; the card's own accent rule already supplies the left edge.
_INDENT = "    "


def args_preview(arguments: dict) -> str:
    """Compact one-line ``k=v`` preview of tool arguments, truncated."""
    parts = [f"{k}={v!r}" for k, v in arguments.items()]
    text = ", ".join(parts)
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + " …"


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    first = lines[0] if lines else ""
    return first if len(first) <= _PREVIEW_CHARS else first[:_PREVIEW_CHARS] + " …"


#: A note's leading source tag — ``[error]``, ``[interrupted]``, ``[mcp]``,
#: ``[workflow]``, ``[envelope] …``. Bounded and anchored so it can only ever
#: match a short leading tag, never swallow a line of model or tool text.
_TAG_RE = re.compile(r"^\[[a-z][a-z0-9 _-]{0,18}\]")

#: Tags that report something went wrong. Matched as substrings of the tag so
#: ``[error]``, ``[command error]`` and ``[seed skipped]`` all land correctly.
_BAD_TAG_WORDS = ("error", "fail", "skipped", "interrupted")


def _note_text(text: str) -> Text:
    """A system note: supporting-detail grey, with its leading ``[tag]`` lifted.

    Notes are the transcript's chorus — mode changes, command output, MCP
    connect results, errors — and rendering all of them at one weight is why a
    long session used to be unskimmable. The tag is the one part that says
    WHERE the line came from, so it gets the emphasis; a tag that reports a
    failure gets the error colour, and everything else stays calm.
    """
    out = Text(text, style=theme.STYLE_MUTED, no_wrap=False)
    match = _TAG_RE.match(text)
    if match is not None:
        tag = match.group(0)
        bad = any(word in tag for word in _BAD_TAG_WORDS)
        out.stylize(theme.STYLE_FAIL if bad else theme.SECONDARY, 0, match.end())
    return out


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

    ``prefix`` is a fixed leader (the user's ``›``) drawn in its own style, so
    the marker can read as chrome while the text reads as content. It is part
    of ``_plain`` because it is part of what the pane shows.
    """

    def __init__(
        self, text: str, *, classes: str, prefix: str = "", prefix_style: str = ""
    ) -> None:
        self._body = text
        self._prefix = prefix
        self._prefix_style = prefix_style
        self._plain = prefix + text
        super().__init__(classes=classes)

    def render(self) -> RenderableType:
        text = Text()
        if self._prefix:
            text.append(self._prefix, style=self._prefix_style or None)
        text.append(self._body)
        return text

    def append(self, text: str) -> None:
        self._body += text
        self._plain = self._prefix + self._body
        self.refresh()


class _Styled(Static):
    """A transcript item whose text is pre-styled and never grows (the
    masthead). Carries the same ``_plain`` mirror as every other item, so
    ``plain_text`` stays a complete read surface."""

    def __init__(self, text: Text, *, classes: str) -> None:
        self._text = text
        self._plain = text.plain
        super().__init__(classes=classes)

    def render(self) -> RenderableType:
        return self._text


class ToolCard(Static):
    """A tool call rendered the moment it is requested, updated through states.

    States: ``requested`` (gate decision known) -> ``awaiting approval`` (an
    ask is pending) -> ``done`` / ``error`` (result) or ``denied`` (user/policy).

    Shape (three ranks of emphasis, so the eye lands on the name and the chip):

        ▏ edit_file   WRITE   awaiting approval
        ▏     path='fib.py', format='search_replace'
        ▏     ✓ ok  wrote 4 line(s)

    The leading rule is a CSS ``border-left`` coloured by risk, NOT a bordered
    box: a full border costs two columns on every card and a scrolling column
    of boxes reads as a form rather than as a log. The rule plus a ``$boost``
    background gives the same grouping for one column and stacks cleanly.

    ``_plain`` is derived from the rendered ``Text`` rather than built beside
    it, so the string tests read can never drift from what is actually drawn.
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
        self._text = Text()
        # Risk colours the accent rule; the state class lets a finished or
        # denied card recede / assert itself without touching the risk hue.
        super().__init__(classes=f"tool-card risk-{risk}")
        self._refresh()

    def render(self) -> RenderableType:
        if self._diff is None:
            return self._text
        # The header/summary plus the colored diff — SPEC §3.1 "tool cards ...
        # diff views". The diff is indented to sit under the summary block.
        return Group(self._text, diff_to_text(self._diff, max_lines=_CARD_DIFF_LINES))

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
        self.set_class(self.state in ("error", "denied"), "state-bad")
        self.set_class(self.state == "awaiting approval", "state-awaiting")
        self._text = self._build()
        self._plain = self._text.plain
        self.refresh()

    def _build(self) -> Text:
        text = Text(no_wrap=False)
        text.append(self.call.name, style=theme.STYLE_TOOL_NAME)
        text.append("  ")
        text.append(theme.risk_chip(self.risk), style=theme.risk_style(self.risk))
        text.append("  ")
        text.append(self.state, style=theme.state_style(self.state))
        # When the diff is shown colored below, drop its raw payload from the
        # one-line arg preview so it isn't printed twice.
        display_args = self.call.arguments
        if self._diff is not None:
            display_args = {k: v for k, v in display_args.items() if k != "edit"}
        preview = args_preview(display_args)
        if preview:
            text.append(f"\n{_INDENT}{preview}", style=theme.STYLE_ARGS)
        if self.result is not None:
            ok = self.result.ok
            text.append(f"\n{_INDENT}")
            mark_style = theme.STYLE_OK if ok else theme.STYLE_FAIL
            text.append("✓ ok" if ok else "✗ error", style=mark_style)
            body = _first_line(self.result.output or self.result.error or "")
            if body:
                text.append(f"  {body}", style=theme.STYLE_RESULT)
        elif self.note:
            text.append(f"\n{_INDENT}")
            text.append("✗ ", style=theme.STYLE_FAIL)
            text.append(_first_line(self.note), style=theme.STYLE_RESULT)
        return text


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
        await self._add(
            _Bubble(text, classes="user", prefix="› ", prefix_style=theme.STYLE_MUTED)
        )

    async def append_assistant(self, text: str) -> None:
        if self._current is None:
            self._current = _Bubble("", classes="assistant")
            await self._add(self._current)
        self._current.append(text)
        self.scroll_end(animate=False)

    def end_assistant(self) -> None:
        """Close the current assistant bubble; the next delta starts a new one."""
        self._current = None

    async def add_masthead(self) -> None:
        """The first thing in an empty transcript: what this is, and the keys
        that get a stranger out of trouble.

        Three lines, IN the transcript rather than over it — it scrolls away
        with the rest of the history instead of being a splash screen that has
        to be dismissed. It replaces the old one-line "IronCore ready" note,
        which said the same things with no hierarchy at all.
        """
        text = Text()
        text.append("IRONCORE", style=theme.STYLE_USER)
        text.append("  a terminal coding agent for open-source models", style=theme.STYLE_MUTED)
        text.append("\n\nType a message to start, or ", style=theme.STYLE_MUTED)
        text.append("/", style=theme.FOREGROUND)
        text.append(" for commands.\n", style=theme.STYLE_MUTED)
        text.append("Shift+Tab", style=theme.FOREGROUND)
        text.append(" cycles the safety mode · ", style=theme.STYLE_MUTED)
        text.append("Esc", style=theme.FOREGROUND)
        text.append(" interrupts · ", style=theme.STYLE_MUTED)
        text.append("Ctrl+C", style=theme.FOREGROUND)
        text.append(" quits.", style=theme.STYLE_MUTED)
        await self._add(_Styled(text, classes="masthead"))

    async def add_mode_note(self, mode: str, description: str) -> None:
        """A mode change, with the mode NAME carrying its autonomy colour.

        The one place a posture change is announced should look like the status
        bar chip it just changed — accept-edits and auto fill, plan and manual
        stay flat (theme.MODE_STYLE). The wording is unchanged, so the plain
        text a reader (or a test) sees is exactly what it always was.
        """
        text = Text()
        text.append("Mode → ", style=theme.STYLE_MUTED)
        text.append(mode, style=theme.mode_style(mode))
        text.append(f": {description}", style=theme.STYLE_MUTED)
        await self._add(_Styled(text, classes="note"))

    async def add_note(self, text: str) -> None:
        """A dim system line: mode changes, command output, errors, interrupts."""
        self._current = None
        await self._add(_Styled(_note_text(text), classes="note"))

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
