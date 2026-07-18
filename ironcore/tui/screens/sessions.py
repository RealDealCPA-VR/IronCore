"""Session picker modal (IC-706, SPEC §11.2).

Lists a workspace's stored session transcripts newest-first so a launch can
resume one. Each row shows a relative age, the session's first-prompt label, and
its user-turn count — everything the picker needs comes from the store's header
metadata (``SessionStore.list_sessions``), never a full transcript read.

The screen resolves purely to a chosen session id via ``dismiss`` (``None`` on
cancel or an empty store); the app's push-callback rehydrates that id into the
live transcript. Like the approval modal, this is a dumb prompt — it reads the
store but never mutates it and holds no engine reference (docs/ARCHITECTURE.md
§4).
"""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView

from ironcore.memory.sessions import SessionRecord, SessionStore
from ironcore.tui import theme

#: Longest first-prompt label shown before it is elided on a row.
_LABEL_MAX = 60


def relative_age(created_at: str, now: datetime | None = None) -> str:
    """A compact "how long ago" for an ISO ``created_at`` (display only).

    Robust to junk (returns ``"?"``) and to naive/aware datetime mismatches, so
    a corrupt or oddly-stamped header never breaks the picker. ``now`` is
    injectable for deterministic tests.
    """
    try:
        then = datetime.fromisoformat(created_at)
    except (ValueError, TypeError):
        return "?"
    reference = now if now is not None else datetime.now(then.tzinfo)
    try:
        seconds = (reference - then).total_seconds()
    except TypeError:  # naive vs aware mismatch — unrankable, don't guess
        return "?"
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _row_parts(record: SessionRecord, now: datetime | None = None) -> tuple[str, str, str]:
    """The three columns of a picker row: right-aligned age, prompt, turn count.

    Split out so the plain string and the styled ``Text`` are assembled from
    one source — a row can never render differently from what ``_row_label``
    reports.
    """
    label = record.first_prompt.strip() or "(no prompt)"
    if len(label) > _LABEL_MAX:
        label = label[: _LABEL_MAX - 1] + "…"
    return f"{relative_age(record.created_at, now):>8}", label, f"{record.turn_count} turn(s)"


def _row_label(record: SessionRecord, now: datetime | None = None) -> str:
    """One picker row: ``  2h ago   fix the parser bug   · 3 turn(s)``."""
    age, label, turns = _row_parts(record, now)
    return f"{age}   {label}   · {turns}"


def _row_text(record: SessionRecord, now: datetime | None = None) -> Text:
    """``_row_label`` with emphasis: the prompt is what you are choosing
    between, so it reads bright; the age and the turn count support it."""
    age, label, turns = _row_parts(record, now)
    text = Text(no_wrap=True)
    text.append(age, style=theme.STYLE_MUTED)
    text.append("   ")
    text.append(label, style=theme.FOREGROUND)
    text.append(f"   · {turns}", style=theme.STYLE_MUTED)
    return text


class SessionPicker(ModalScreen[str | None]):
    """Pick a stored session to resume; dismisses with its id (``None`` = cancel)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    def __init__(self, store: SessionStore) -> None:
        super().__init__()
        self.store = store
        #: Snapshot taken once at construction — newest-first (store contract).
        self.records: list[SessionRecord] = store.list_sessions()

    def compose(self) -> ComposeResult:
        box = Vertical(id="session-box")
        box.border_title = "Resume a session"
        box.border_subtitle = "enter resumes · esc starts fresh"
        with box:
            if not self.records:
                yield Label(
                    Text("No sessions yet — press Esc to start fresh."),
                    id="session-empty",
                )
                return
            items = [
                ListItem(Label(_row_text(record)), name=record.id)
                for record in self.records
            ]
            yield ListView(*items, id="session-list")

    def on_mount(self) -> None:
        # Focus after refresh: the composed ListView is not mounted yet when a
        # screen's on_mount fires, so query it once the DOM has settled.
        if self.records:
            self.call_after_refresh(self._focus_list)

    def _focus_list(self) -> None:
        try:
            self.query_one("#session-list", ListView).focus()
        except NoMatches:  # dismissed before the list settled — nothing to focus
            pass

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """A row was chosen (Enter or click): resolve with its session id."""
        self.dismiss(event.item.name)

    def action_cancel(self) -> None:
        self.dismiss(None)
