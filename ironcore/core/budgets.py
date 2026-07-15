"""Budgets + runaway protection for the turn engine (SPEC §5.6, SAFETY.md T5).

``Budget`` is the production :class:`~ironcore.core.protocols.BudgetTracker`
(IC-506); it replaces ``protocols.DefaultBudget``. It bounds a single turn (and
the whole session) so a confused model cannot burn tokens or spin forever. Every
tripped cap ends the turn CLEANLY with the stop_reason ``"budget"`` (the engine's
event vocabulary has no ``"loop"``) — a cap is a returned stop_reason, NEVER an
exception.

Per-TURN caps (reset by :meth:`Budget.start_turn`):

* ``max_provider_calls`` — provider calls this turn.
* ``max_tokens``         — cumulative tokens this turn.
* ``max_seconds``        — wall-clock seconds this turn, measured with a
  monotonic clock that is injectable for deterministic tests.
* ``max_repairs``        — repair attempts this turn.

Per-SESSION caps (NOT reset by ``start_turn``; ``None`` = unlimited):

* ``max_session_calls``  — cumulative provider calls across the session.
* ``max_session_tokens`` — cumulative tokens across the session.

``check()`` returns ``"budget"`` when ANY of the above is met, else ``None``;
``should_continue()`` is its cheap boolean face for ``sampling.best_of``.

Loop detection (:meth:`Budget.note_tool`) — the runaway guard of SPEC §5.6:
identical consecutive ``(tool name, canonical-json args)`` calls are a loop
signal.

* 2nd identical call in a row → INTERVENTION frame: recorded, returns ``None``.
  The engine has no separate intervention channel, so this is a no-op stop-wise
  and is surfaced only through :meth:`summary`.
* 3rd identical call in a row (``loop_limit``) → STOP with ``"budget"``.

A different ``(name, args)`` resets the streak. Stdlib only; this module never
imports the engine (the engine imports the Protocol, not this concrete class).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # annotations only — no runtime coupling to config
    from ironcore.config.settings import Settings

__all__ = ["Budget"]

#: The single stop_reason both caps and the loop detector report; the engine
#: writes it verbatim to ``TurnCompleted.stop_reason``.
_STOP = "budget"


def _canonical_args(args: Any) -> str:
    """Stable string form of tool args so equal calls compare equal.

    Falls back to ``repr`` for anything JSON cannot encode; ``default=str``
    means this never raises on exotic argument values.
    """
    try:
        return json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(args)


class Budget:
    """Per-turn + per-session budgets with an identical-tool-call loop detector.

    See the module docstring for the cap list and the 2×-warn / 3×-stop loop
    rule. A ``None`` session cap (or ``max_tokens`` / ``max_seconds`` / a ``None``
    ``max_repairs``) means "unlimited". Deterministic given the injected
    ``clock``; no method ever raises.
    """

    def __init__(
        self,
        *,
        max_provider_calls: int = 20,
        max_tokens: int | None = 150_000,
        max_seconds: float | None = 300.0,
        max_repairs: int | None = 3,
        max_session_calls: int | None = None,
        max_session_tokens: int | None = None,
        loop_limit: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_provider_calls = max_provider_calls
        self.max_tokens = max_tokens
        self.max_seconds = max_seconds
        self.max_repairs = max_repairs
        self.max_session_calls = max_session_calls
        self.max_session_tokens = max_session_tokens
        self.loop_limit = loop_limit
        self._clock = clock
        # per-SESSION counters — survive start_turn().
        self._session_calls = 0
        self._session_tokens = 0
        # per-TURN counters — reset by start_turn().
        self._turn_calls = 0
        self._turn_tokens = 0
        self._turn_repairs = 0
        self._turn_start = clock()
        # loop detector — reset by start_turn().
        self._last_tool_key: tuple[str, str] | None = None
        self._tool_repeat = 0
        self._interventions = 0

    # -- lifecycle ----------------------------------------------------------- #

    def start_turn(self) -> None:
        """Reset per-TURN counters + the loop detector; session totals persist."""
        self._turn_calls = 0
        self._turn_tokens = 0
        self._turn_repairs = 0
        self._turn_start = self._clock()
        self._last_tool_key = None
        self._tool_repeat = 0
        self._interventions = 0

    def record_call(self, tokens: int) -> None:
        """Count one provider call and its token cost (turn AND session)."""
        cost = max(0, int(tokens))
        self._turn_calls += 1
        self._turn_tokens += cost
        self._session_calls += 1
        self._session_tokens += cost

    def note_repair(self) -> str | None:
        """Count one repair attempt this turn; return the stop_reason if that
        reaches ``max_repairs``, else ``None``.

        NOT part of the ``BudgetTracker`` Protocol: the engine owns its repair
        loop (via ``RepairPolicy``) and does not call this today, so
        ``max_repairs`` is a latent cap until the engine reports repairs here.
        Exposed for direct callers and unit coverage.
        """
        self._turn_repairs += 1
        return self.check()

    # -- gates --------------------------------------------------------------- #

    def check(self) -> str | None:
        """``"budget"`` if ANY turn/session cap is met, else ``None``."""
        if self._turn_calls >= self.max_provider_calls:
            return _STOP
        if self.max_tokens is not None and self._turn_tokens >= self.max_tokens:
            return _STOP
        if self.max_seconds is not None and self._elapsed() >= self.max_seconds:
            return _STOP
        if self.max_repairs is not None and self._turn_repairs >= self.max_repairs:
            return _STOP
        if self.max_session_calls is not None and self._session_calls >= self.max_session_calls:
            return _STOP
        if (
            self.max_session_tokens is not None
            and self._session_tokens >= self.max_session_tokens
        ):
            return _STOP
        return None

    def note_tool(self, name: str, args: dict[str, Any]) -> str | None:
        """Loop detector: on the ``loop_limit``-th identical consecutive
        ``(name, args)`` call return ``"budget"``; the one before it is an
        intervention frame (recorded, returns ``None``); a different call resets
        the streak.
        """
        key = (name, _canonical_args(args))
        if key == self._last_tool_key:
            self._tool_repeat += 1
        else:
            self._last_tool_key = key
            self._tool_repeat = 1
        if self._tool_repeat >= self.loop_limit:
            return _STOP
        if self._tool_repeat == self.loop_limit - 1:
            self._interventions += 1  # 2× (of 3): intervention, no stop channel
        return None

    def should_continue(self) -> bool:
        """Cheap "still under budget?" for ``sampling.best_of`` / inner loops."""
        return self.check() is None

    # -- reporting ----------------------------------------------------------- #

    def summary(self) -> dict[str, Any]:
        """What the current turn (and the session) has spent — for
        ``TurnCompleted.usage`` / a future ``state.budgets_spent`` write."""
        return {
            "calls": self._turn_calls,
            "tokens": self._turn_tokens,
            "elapsed": round(self._elapsed(), 3),
            "repairs": self._turn_repairs,
            "interventions": self._interventions,
            "session_calls": self._session_calls,
            "session_tokens": self._session_tokens,
        }

    # -- construction from config -------------------------------------------- #

    @classmethod
    def from_settings(cls, settings: Settings) -> Budget:
        """Build from settings, reading an optional ``budgets`` section if the
        config ever grows one; absent/``None`` fields fall back to the
        constructor defaults. Tolerant by design so it keeps working before
        ``settings.py`` declares any budget fields.
        """
        section = getattr(settings, "budgets", None)
        if section is None:
            return cls()

        def _or_default(name: str, default: Any) -> Any:
            value = getattr(section, name, None)
            return default if value is None else value

        return cls(
            max_provider_calls=_or_default("max_provider_calls", 20),
            max_tokens=_or_default("max_tokens", 150_000),
            max_seconds=_or_default("max_seconds", 300.0),
            max_repairs=_or_default("max_repairs", 3),
            max_session_calls=getattr(section, "max_session_calls", None),
            max_session_tokens=getattr(section, "max_session_tokens", None),
            loop_limit=_or_default("loop_limit", 3),
        )

    # -- internals ----------------------------------------------------------- #

    def _elapsed(self) -> float:
        return self._clock() - self._turn_start
