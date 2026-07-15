"""Session store: append-only JSONL transcripts you can list, load, and resume.

Companion to ``core/state.py`` — that module owns the small per-session *state*
JSON (mode/goal/plan/cursor); THIS module owns the full *transcript*: every user
turn, assistant reply, and engine event, one per line, so a session can be picked
from a list (IC-706) and rehydrated into a live conversation.

Rules of this module (docs/ARCHITECTURE.md §5, docs/SPEC.md §11.2):

* One file per session at ``<workspace>/.ironcore/sessions/<id>.jsonl``. The id
  and every timestamp are PASSED IN by the caller — there is deliberately no
  ``datetime.now`` here, so the store is fully deterministic under test.
* One JSON object per line, ``{"kind": ..., <payload>}``. The FIRST line is a
  ``header`` carrying id/created_at/first_prompt — all the metadata the picker
  needs without reading the body.
* Append-only, crash-safe like ``ironcore/safety/audit.py``: writes open in "a"
  mode, emit exactly one ``json.dumps`` + newline, and flush; a crash loses at
  most the in-flight line. There is no rewrite/truncate path for existing lines.
* Corruption-tolerant and never raises on read: a single bad line is skipped and
  counted (reported via the module logger); a file whose header will not parse is
  skipped whole by listing.
* Size-capped: at most ``max_sessions`` files are kept; the oldest beyond the cap
  are pruned, but the session currently being written is never deleted.
* CRLF-safe: lines are written with ``newline="\\n"`` and read via ``splitlines``.
* Stdlib + ``ironcore.providers.base`` only (memory-package + task rule).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ironcore.providers.base import Message

logger = logging.getLogger(__name__)

#: On-disk schema marker written into every header line. Additive for now.
SCHEMA_VERSION = 1

SESSIONS_DIRNAME = "sessions"

#: Default ceiling on stored sessions before the oldest are pruned.
DEFAULT_MAX_SESSIONS = 200

# Line kinds — the "kind" discriminator on every JSONL record.
_KIND_HEADER = "header"
_KIND_USER = "user"
_KIND_ASSISTANT = "assistant"
_KIND_EVENT = "event"

# rehydrate() tail-summary tuning (display only).
_TAIL_MESSAGES = 4
_TAIL_PREVIEW = 80


def sessions_dir(workspace: str | Path) -> Path:
    """Canonical directory holding a workspace's session transcripts."""
    return Path(workspace) / ".ironcore" / SESSIONS_DIRNAME


def _validate_id(session_id: str) -> str:
    """Reject ids that could escape the sessions directory (they name a file)."""
    if not session_id or session_id in (".", "..") or any(
        sep in session_id for sep in ("/", "\\", "..")
    ):
        raise ValueError(f"invalid session id: {session_id!r}")
    return session_id


def _preview(text: object) -> str:
    """Whitespace-collapsed, length-capped one-liner for the tail summary."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) > _TAIL_PREVIEW:
        collapsed = collapsed[: _TAIL_PREVIEW - 1] + "…"
    return collapsed


def _tail_summary(messages: list[Message]) -> str:
    """A short human blurb of the last few messages, shown when resuming."""
    if not messages:
        return "empty session — no messages yet"
    tail = messages[-_TAIL_MESSAGES:]
    lines = [f"{m.role}: {_preview(m.content)}" for m in tail]
    return f"{len(messages)} message(s); tail:\n" + "\n".join(lines)


@dataclass
class SessionRecord:
    """Picker-facing metadata for one stored session (SPEC §11.2)."""

    id: str
    created_at: str  # iso string, passed in by the caller
    turn_count: int  # number of user turns recorded in the transcript
    first_prompt: str  # label for the picker
    path: Path


class SessionStore:
    """Reads and writes session transcripts under one workspace.

    The store never invents time: ``create`` takes both the stable ``session_id``
    (used verbatim as the filename stem) and its ``created_at`` iso stamp. The
    session most recently created or appended-to is treated as *active* and is
    exempt from pruning.
    """

    def __init__(
        self, workspace: str | Path, *, max_sessions: int = DEFAULT_MAX_SESSIONS
    ) -> None:
        self.dir = sessions_dir(workspace)
        self.max_sessions = max(1, int(max_sessions))
        self._active_id: str | None = None
        self._lock = threading.Lock()

    # -- paths ---------------------------------------------------------------

    def path_for(self, session_id: str) -> Path:
        """Transcript file for ``session_id`` (validates the id first)."""
        return self.dir / f"{_validate_id(session_id)}.jsonl"

    # -- writing -------------------------------------------------------------

    def create(
        self, session_id: str, created_at: str, first_prompt: str = ""
    ) -> SessionRecord:
        """Start a new session by writing its header line, then prune to the cap.

        Raises ``ValueError`` if a session with this id already exists — creating
        is one-shot per id; subsequent lines go through the ``append_*`` methods.
        """
        path = self.path_for(session_id)
        if path.exists():
            raise ValueError(f"session already exists: {session_id!r}")
        self._append(
            session_id,
            {
                "kind": _KIND_HEADER,
                "v": SCHEMA_VERSION,
                "id": session_id,
                "created_at": created_at,
                "first_prompt": first_prompt,
            },
        )
        self.prune()
        return SessionRecord(
            id=session_id,
            created_at=created_at,
            turn_count=0,
            first_prompt=first_prompt,
            path=path,
        )

    def append_user(self, session_id: str, text: str) -> None:
        """Append a user turn."""
        self._append(session_id, {"kind": _KIND_USER, "text": text})

    def append_assistant(self, session_id: str, text: str) -> None:
        """Append an assistant reply."""
        self._append(session_id, {"kind": _KIND_ASSISTANT, "text": text})

    def append_event(self, session_id: str, event_dict: dict[str, Any]) -> None:
        """Append a typed engine event; its dict lands under ``payload``."""
        self._append(session_id, {"kind": _KIND_EVENT, "payload": dict(event_dict)})

    def _append(self, session_id: str, obj: dict[str, Any]) -> None:
        path = self.path_for(session_id)
        line = json.dumps(obj, ensure_ascii=False, default=str)
        with self._lock:
            self._active_id = session_id
            path.parent.mkdir(parents=True, exist_ok=True)
            # crash safety: the whole line + newline in one flushed write
            with path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
                fh.flush()

    # -- reading -------------------------------------------------------------

    def load(self, session_id: str) -> list[dict[str, Any]]:
        """Every parsed line of a session, in order (header included).

        Corrupt or non-object lines are skipped and the skip count is logged. A
        missing/unreadable session yields ``[]`` — reads never raise.
        """
        lines = self._read_lines(self.path_for(session_id))
        if lines is None:
            return []
        records: list[dict[str, Any]] = []
        skipped = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                skipped += 1
                continue
            if isinstance(obj, dict):
                records.append(obj)
            else:
                skipped += 1
        if skipped:
            logger.warning("session %s: skipped %d corrupt line(s)", session_id, skipped)
        return records

    def rehydrate(self, session_id: str) -> tuple[list[Message], str]:
        """Reconstruct the conversation ``Message`` list plus a tail summary.

        Only user/assistant lines become messages; header and event lines are
        transcript bookkeeping, not conversation, so they are left out.
        """
        messages: list[Message] = []
        for obj in self.load(session_id):
            kind = obj.get("kind")
            if kind == _KIND_USER:
                messages.append(Message(role="user", content=str(obj.get("text", ""))))
            elif kind == _KIND_ASSISTANT:
                messages.append(Message(role="assistant", content=str(obj.get("text", ""))))
        return messages, _tail_summary(messages)

    def list_sessions(self) -> list[SessionRecord]:
        """All valid sessions, newest first (created_at desc, then id desc).

        Prunes to the cap first, then reads each file's header for metadata;
        a file whose header will not parse is skipped whole.
        """
        self.prune()
        records: list[SessionRecord] = []
        if self.dir.is_dir():
            for path in self.dir.glob("*.jsonl"):
                record = self._read_record(path)
                if record is not None:
                    records.append(record)
        records.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return records

    def prune(self) -> int:
        """Delete the oldest sessions beyond ``max_sessions``; return the count.

        The active session (most recently created/appended) is never deleted,
        even if it is the oldest — protecting it may leave one file over the cap.
        Corrupt files are left untouched (they cannot be ordered).
        """
        if not self.dir.is_dir():
            return 0
        valid = [
            record
            for path in self.dir.glob("*.jsonl")
            if (record := self._read_record(path)) is not None
        ]
        if len(valid) <= self.max_sessions:
            return 0
        valid.sort(key=lambda r: (r.created_at, r.id), reverse=True)  # newest first
        pruned = 0
        for record in valid[self.max_sessions :]:  # everything past the cap = oldest
            if record.id == self._active_id:
                continue  # never delete the session being written
            try:
                record.path.unlink()
            except OSError:
                continue
            pruned += 1
        if pruned:
            logger.info(
                "pruned %d old session(s) beyond max_sessions=%d", pruned, self.max_sessions
            )
        return pruned

    # -- internals -----------------------------------------------------------

    def _read_lines(self, path: Path) -> list[str] | None:
        """Return a file's lines (CRLF-safe), or None if it cannot be read."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return text.splitlines()

    def _read_record(self, path: Path) -> SessionRecord | None:
        """Header metadata + user-turn count, or None if the file is unusable.

        The header must be the FIRST non-empty line and parse as a valid header
        object; otherwise the whole file is skipped.
        """
        lines = self._read_lines(path)
        if lines is None:
            return None
        header: dict[str, Any] | None = None
        body_start = 0
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            body_start = index + 1
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                return None  # corrupt header -> skip whole file
            if not (
                isinstance(obj, dict)
                and obj.get("kind") == _KIND_HEADER
                and "id" in obj
                and "created_at" in obj
            ):
                return None
            header = obj
            break
        if header is None:
            return None  # empty file
        turn_count = 0
        for line in lines[body_start:]:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict) and obj.get("kind") == _KIND_USER:
                turn_count += 1
        return SessionRecord(
            id=str(header["id"]),
            created_at=str(header["created_at"]),
            turn_count=turn_count,
            first_prompt=str(header.get("first_prompt", "")),
            path=path,
        )
