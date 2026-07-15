"""LadderRepairPolicy — the SPEC §5.4 repair loop (IC-503).

Replaces ``core.protocols.DefaultRepairPolicy`` (retry-once-then-give-up) with
the full ladder-down behaviour SPEC §5.4 describes: a malformed tool call is
re-asked once with the mechanical error framed as feedback; a second failure at
the same rung walks one rung *down* toward the always-works text floor; a
failure at the floor surfaces to the user. Repairs are budgeted (a hard
per-turn attempt cap) and the engine renders each as a dimmed ``[repair]``
transcript entry, so they are never silent.

Design: the policy is a **pure, stateless** function of ``(attempt, rung)``. It
holds no cross-turn state, so it is safe for the engine to construct once and
reuse for every turn (the engine never calls ``reset``). ``attempt`` is the
0-based count of repairs already made THIS turn (the engine bumps it after each
RETRY/LADDER_DOWN); ``rung`` is the active tool-protocol rung, which the engine
forces to the text floor once it has laddered down.

Decision table (evaluated top to bottom; first match wins)::

    attempt >= max_attempts              -> GIVE_UP      # per-turn repair cap (default 4)
    attempt == 0                         -> RETRY        # 1st failure at this rung: re-ask once
    attempt >= 1 and rung == text floor  -> GIVE_UP      # floor failure surfaces to the user
    attempt >= 1 and rung != text floor  -> LADDER_DOWN  # 2nd failure above the floor: drop a rung

Worked sequences (the engine bumps ``attempt`` after RETRY/LADDER_DOWN and, on
LADDER_DOWN, pins ``rung`` to the text floor for the rest of the turn):

* start at a native/strict rung — malformed, malformed, malformed:
  ``(0, native)→RETRY`` · ``(1, native)→LADDER_DOWN`` (rung→floor) ·
  ``(2, text_protocol)→GIVE_UP``. The floor gets exactly one shot — the
  ladder-down re-ask itself — then surfaces.
* start already at the floor — malformed, malformed:
  ``(0, text_protocol)→RETRY`` · ``(1, text_protocol)→GIVE_UP``.

``frame_error`` turns a mechanical parser/patcher message into crisp,
model-facing feedback: what went wrong, the offending text, and a
rung-specific reminder of the correct call format. It is pure and unit-tested;
the engine frames inline today and can adopt it with a one-line change.

Stdlib only (plus the ``RepairAction`` enum). No engine import — the engine
imports this, never the reverse.
"""

from __future__ import annotations

from ironcore.core.protocols import RepairAction, RepairPolicy

__all__ = ["TEXT_PROTOCOL_FLOOR", "LadderRepairPolicy", "frame_error"]

#: The always-works bottom rung of the tool-call ladder (envelope §4.3). Kept as
#: a literal — matching the string the engine and ``profile.TOOL_PROTOCOL_LADDER``
#: use — so ``repair`` stays a stdlib-only leaf with no envelope import.
TEXT_PROTOCOL_FLOOR = "text_protocol"

#: Longest raw fragment echoed back in framed feedback; keeps the re-ask bounded
#: even when ``raw`` is a whole accumulated completion.
_MAX_RAW_ECHO = 500

#: Default per-turn repair-attempt cap (SPEC §5.6 lists repair attempts among the
#: per-turn budgets). Normal ladders terminate well before this via the floor
#: GIVE_UP; the cap is a hard backstop that guarantees the loop always ends.
_DEFAULT_MAX_ATTEMPTS = 4


class LadderRepairPolicy(RepairPolicy):
    """Retry once, then ladder down toward the text floor, then give up (§5.4).

    Pure and deterministic: :meth:`decide` is a function of ``attempt`` and
    ``rung`` alone, so one instance is reused across every turn with no state to
    leak or reset. ``max_attempts`` bounds total repairs per turn regardless of
    rung — once ``attempt`` reaches it the answer is always ``GIVE_UP``.
    """

    def __init__(self, *, max_attempts: int = _DEFAULT_MAX_ATTEMPTS) -> None:
        self.max_attempts = max_attempts

    def decide(self, *, attempt: int, error: str, raw: str, rung: str) -> RepairAction:
        """Choose RETRY / LADDER_DOWN / GIVE_UP per the module decision table."""
        if attempt >= self.max_attempts:
            return RepairAction.GIVE_UP
        if attempt == 0:
            # First failure at this rung: re-ask once with the error as feedback.
            return RepairAction.RETRY
        # A re-ask has already happened at this rung and failed again.
        if rung == TEXT_PROTOCOL_FLOOR:
            # No lower rung exists — a floor failure surfaces to the user.
            return RepairAction.GIVE_UP
        # Above the floor: drop one rung toward the always-works text protocol.
        return RepairAction.LADDER_DOWN


def frame_error(error: str, raw: str, rung: str) -> str:
    """Frame a mechanical repair error as crisp, model-facing feedback.

    Pure. Produces three parts: what went wrong (``error``), the offending text
    (``raw``, bounded), and a rung-specific reminder of the correct call format
    so the model knows exactly how to re-issue the call. ``error`` and ``raw``
    may be empty; the output is always non-empty and actionable.
    """
    reason = (error or "").strip() or "it was malformed or could not be applied"
    lines = [f"Your previous tool call could not be used: {reason}"]
    snippet = _echo(raw)
    if snippet:
        lines.append(f"The unusable text was:\n{snippet}")
    lines.append(_format_reminder(rung))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _echo(raw: str) -> str:
    """The offending text, whitespace-trimmed and length-bounded for the re-ask."""
    text = (raw or "").strip()
    if not text:
        return ""
    if len(text) <= _MAX_RAW_ECHO:
        return text
    dropped = len(text) - _MAX_RAW_ECHO
    return f"{text[:_MAX_RAW_ECHO]}… [{dropped} more chars omitted]"


def _format_reminder(rung: str) -> str:
    """A correct-format reminder naming the active rung's tool-call format."""
    if rung == TEXT_PROTOCOL_FLOOR:
        return (
            "Fix it and reply with EXACTLY ONE fenced ```ironcall block whose body "
            'is a single JSON object {"tool": "<name>", "args": {...}} and nothing '
            "after the block (use {} for a tool that takes no arguments)."
        )
    if rung == "strict_json":
        return (
            "Fix it and re-issue the call as a single strict JSON object with valid "
            "syntax — no trailing commas, no comments, all keys and strings quoted."
        )
    if rung == "native":
        return (
            "Fix it and re-issue the call using the native function-calling format "
            "with a complete, valid JSON arguments object."
        )
    # Unknown / future rung: stay generic but still actionable.
    return (
        f"Fix it and re-issue the call using the {rung!r} tool-call format with "
        "valid, complete JSON arguments."
    )
