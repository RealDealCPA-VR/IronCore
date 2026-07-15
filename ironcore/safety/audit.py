"""Append-only audit trail writer (docs/SAFETY.md §5).

Every gate decision, tool call, approval, mode change, and turn end lands as
one JSON object per line in ``<workspace>/.ironcore/audit/YYYY-MM-DD.jsonl``.
Rules this module enforces:

- Append only. Files are opened in "a" mode for every write, and there is
  deliberately NO delete/rewrite/truncate API anywhere in this module —
  adding one would violate SAFETY.md §7.
- Tool args never land on disk: only a sha256 fingerprint plus a human
  preview hard-capped at ``PREVIEW_MAX`` characters.
- Crash safety: one ``json.dumps`` per line + newline + flush; nothing is
  buffered across events, so a crash loses at most the in-flight line.
- Stdlib only (safety package rule, docs/ARCHITECTURE.md §4).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

#: Hard cap on the human-readable args preview, in characters.
PREVIEW_MAX = 120

#: The only event types an audit line may carry (docs/SAFETY.md §5).
EVENT_TYPES: frozenset[str] = frozenset(
    {"tool_call", "gate", "approval", "mode_change", "turn_end"}
)


def fingerprint_args(args: object) -> tuple[str, str]:
    """Return ``(sha256 hex, preview)`` for tool args; the pair is all that is logged.

    Dicts are canonicalized with sorted keys so the hash is stable across key
    order. The preview is whitespace-collapsed and truncated to PREVIEW_MAX.
    """
    text = _canonical(args)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    preview = " ".join(text.split())  # one line, human-scannable
    if len(preview) > PREVIEW_MAX:
        preview = preview[: PREVIEW_MAX - 1] + "…"
    return digest, preview


def _canonical(args: object) -> str:
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # unsortable mixed-type keys and other exotica
        return repr(args)


def _as_utc(ts: datetime) -> datetime:
    # naive timestamps are taken to already be UTC — never local time
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


class AuditWriter:
    """Appends audit events for one session. The only verb here is append.

    ``clock`` exists for tests; production uses now-UTC. A per-call ``ts``
    override on every method beats the clock, so replayed events can carry
    their true time.
    """

    def __init__(
        self,
        workspace: str | Path,
        session: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.audit_dir = Path(workspace) / ".ironcore" / "audit"
        self._clock = clock or (lambda: datetime.now(UTC))

    def path_for(self, ts: datetime) -> Path:
        """Audit file for the UTC day containing ``ts``."""
        return self.audit_dir / f"{_as_utc(ts):%Y-%m-%d}.jsonl"

    def write(
        self, event: Mapping[str, object], *, ts: datetime | None = None
    ) -> dict[str, object]:
        """Append one event line and return the record exactly as written.

        ``event`` must carry an ``event`` key naming one of EVENT_TYPES and an
        integer ``turn`` — unknown types are refused (fail closed), not logged.
        """
        kind = event.get("event")
        if kind not in EVENT_TYPES:
            raise ValueError(f"unknown audit event type: {kind!r}")
        if not isinstance(event.get("turn"), int):
            raise ValueError("audit event needs an integer 'turn'")
        stamp = _as_utc(ts) if ts is not None else _as_utc(self._clock())
        record: dict[str, object] = {"ts": stamp.isoformat(), "session": self.session, **event}
        line = json.dumps(record, ensure_ascii=False, default=str)
        path = self.path_for(stamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        # crash safety: the whole line + newline in one write, flushed before close
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
        return record

    def tool_call(
        self,
        turn: int,
        tool: str,
        args: object,
        status: str,
        *,
        ts: datetime | None = None,
    ) -> dict[str, object]:
        """A completed tool execution; ``status`` is the honest result status."""
        digest, preview = fingerprint_args(args)
        return self.write(
            {
                "event": "tool_call",
                "turn": turn,
                "tool": str(tool),
                "args_sha256": digest,
                "args_preview": preview,
                "status": str(status),
            },
            ts=ts,
        )

    def gate(
        self,
        turn: int,
        tool: str,
        args: object,
        decision: str,
        *,
        ts: datetime | None = None,
    ) -> dict[str, object]:
        """A policy gate decision — allow/ask/deny — for an attempted tool call."""
        digest, preview = fingerprint_args(args)
        return self.write(
            {
                "event": "gate",
                "turn": turn,
                "tool": str(tool),
                "args_sha256": digest,
                "args_preview": preview,
                "decision": str(decision),
            },
            ts=ts,
        )

    def approval(
        self,
        turn: int,
        tool: str,
        answer: str,
        reason: str | None = None,
        *,
        ts: datetime | None = None,
    ) -> dict[str, object]:
        """A human's answer to an ask gate: "approve" or "deny", optional reason."""
        return self.write(
            {
                "event": "approval",
                "turn": turn,
                "tool": str(tool),
                "answer": str(answer),
                "reason": reason,
            },
            ts=ts,
        )

    def mode_change(
        self,
        turn: int,
        from_mode: str,
        to_mode: str,
        *,
        ts: datetime | None = None,
    ) -> dict[str, object]:
        """An autonomy mode transition (Shift+Tab or /mode)."""
        return self.write(
            {
                "event": "mode_change",
                "turn": turn,
                "from_mode": str(from_mode),
                "to_mode": str(to_mode),
            },
            ts=ts,
        )

    def turn_end(
        self,
        turn: int,
        stop_reason: str | None = None,
        *,
        ts: datetime | None = None,
    ) -> dict[str, object]:
        """End of a turn; ``stop_reason`` is the evidence-based reason (SPEC §3.4)."""
        return self.write(
            {"event": "turn_end", "turn": turn, "stop_reason": stop_reason},
            ts=ts,
        )
