"""Approval flow plumbing between the turn engine and a front end (IC-403).

When the gate says ``ask`` (SPEC §5, SAFETY §3), the engine awaits
``ApprovalBroker.request(...)`` and a front end — the TUI modal (IC-703),
a headless policy, a test — resolves it with ``answer(request_id, ...)``.
Rules this module enforces:

- **Fail closed.** An unanswered request DENIES after ``timeout`` seconds;
  a crashing ``on_request`` callback denies immediately (SAFETY §1).
- **Grants are turn-scoped, never longer** (SAFETY §3). "Approve all writes
  this turn" records a grant that ``begin_turn``/``end_turn`` clear; each
  grant also remembers its turn number, so a stale grant can never cover a
  request from another turn even if a lifecycle call is missed.
- **Nothing invisible.** With an ``AuditWriter`` attached, every resolved
  answer — explicit, grant-auto-approved, or timeout-denied — lands as one
  ``approval`` audit line (SAFETY §5). The audit "tool" field is the
  request ``key`` when given (the engine passes the tool name), else the
  risk class. ``audit=None`` disables auditing; nothing else changes.
- **UI-agnostic.** The broker never prints or prompts. Front-end signalling
  is an injected async ``on_request`` callback — chosen over a queue so the
  request id travels with the signal and there is exactly one consumer to
  reason about; event-stream-driven front ends may poll ``pending()``
  instead and call ``answer()`` directly.
- **Single event loop.** Futures are plain ``asyncio.Future``s; every
  method must run on the loop that called ``request()``. Cross-thread
  front ends must hop via ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ironcore.safety.audit import AuditWriter

#: Fail-closed ceiling on how long an unanswered request may block the engine.
DEFAULT_TIMEOUT = 300.0


@dataclass(frozen=True)
class ApprovalAnswer:
    """A human's (or policy's) verdict on one approval request.

    ``scope="turn"`` on an *approve* records a grant covering matching
    requests for the rest of the current turn ("approve all writes this
    turn", SPEC §3.1). Scope is meaningless on a deny and is ignored.
    A deny ``reason`` is optional and fed back to the model verbatim
    (SAFETY §4).
    """

    decision: Literal["approve", "deny"]
    reason: str | None = None
    scope: Literal["once", "turn"] = "once"


@dataclass(frozen=True)
class ApprovalRequest:
    """Everything a front end needs to render an approval modal and answer it."""

    id: str
    preview: str  # the exact effect: full diff / command line / URL (SAFETY §4)
    risk: str
    turn: int
    key: str | None = None  # optional narrowing — typically the tool name


@dataclass(frozen=True)
class _Grant:
    """A live "approve all matching asks this turn" grant."""

    turn: int
    risk: str
    key: str | None  # None = covers every request of this risk this turn

    def covers(self, request: ApprovalRequest) -> bool:
        # turn equality makes stale grants inert even without end_turn();
        # a keyed grant covers only the same key, a key-less grant is risk-wide
        return (
            self.turn == request.turn
            and self.risk == request.risk
            and (self.key is None or self.key == request.key)
        )


class ApprovalBroker:
    """Mediates ask-gate approvals: the engine asks, a front end answers.

    Engine side::

        answer = await broker.request(preview, risk="write", turn=3, key="write_file")

    Front-end side (from the ``on_request`` callback or a ``pending()`` poll)::

        broker.answer(request.id, ApprovalAnswer(decision="approve", scope="turn"))
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        on_request: Callable[[ApprovalRequest], Awaitable[None]] | None = None,
        audit: AuditWriter | None = None,
    ) -> None:
        self.timeout = timeout
        self.on_request = on_request
        self.audit = audit
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Future[ApprovalAnswer]]] = {}
        self._grants: list[_Grant] = []
        self._turn: int | None = None
        self._ids = itertools.count(1)

    # -- turn lifecycle -------------------------------------------------------

    def begin_turn(self, turn: int) -> None:
        """Mark a new turn. Grants never survive a turn boundary (SAFETY §3)."""
        self._turn = turn
        self._grants.clear()

    def end_turn(self) -> None:
        """Expire every turn-scoped grant."""
        self._grants.clear()

    @property
    def current_turn(self) -> int | None:
        """Turn number from the last ``begin_turn`` — informational only."""
        return self._turn

    # -- engine side ----------------------------------------------------------

    async def request(
        self,
        preview: str,
        *,
        risk: str,
        turn: int,
        key: str | None = None,
    ) -> ApprovalAnswer:
        """Block until a covering grant, a front-end answer, or timeout.

        A live turn-scoped grant matching this risk/key auto-approves without
        signalling the front end. Otherwise the request is parked as pending,
        ``on_request`` (if set) is awaited, and the answer arrives via
        ``answer()``. No answer within ``self.timeout`` seconds → DENY
        (fail closed). Every returned answer is audited when ``audit`` is set.
        """
        request = ApprovalRequest(
            id=f"apr-{next(self._ids)}", preview=preview, risk=risk, turn=turn, key=key
        )
        if any(grant.covers(request) for grant in self._grants):
            return self._resolve(
                request,
                ApprovalAnswer(
                    decision="approve", reason="covered by turn-scoped grant", scope="turn"
                ),
            )

        future: asyncio.Future[ApprovalAnswer] = asyncio.get_running_loop().create_future()
        self._pending[request.id] = (request, future)
        try:
            if self.on_request is not None:
                try:
                    await self.on_request(request)
                except Exception as exc:  # a broken approval channel must not hang the engine
                    if future.done() and not future.cancelled():
                        return self._resolve(request, future.result())  # answered, then crashed
                    return self._resolve(
                        request,
                        ApprovalAnswer(
                            decision="deny",
                            reason=f"approval channel failed ({exc}) — denied (fail closed)",
                        ),
                    )
            try:
                answer = await asyncio.wait_for(future, self.timeout)
            except TimeoutError:
                answer = ApprovalAnswer(
                    decision="deny",
                    reason=f"no approval within {self.timeout:g}s — denied (fail closed)",
                )
            return self._resolve(request, answer)
        finally:
            # covers timeout AND engine-side cancellation (Esc interrupts the turn)
            self._pending.pop(request.id, None)

    # -- front-end side ---------------------------------------------------------

    def answer(self, request_id: str, answer: ApprovalAnswer) -> None:
        """Resolve a pending request. A ``scope="turn"`` approve records a grant.

        Raises ``KeyError`` when ``request_id`` is not pending — already
        answered, timed out, or cancelled. Front ends racing the timeout
        must handle it.
        """
        try:
            request, future = self._pending.pop(request_id)
        except KeyError:
            raise KeyError(f"no pending approval request {request_id!r}") from None
        if answer.decision == "approve" and answer.scope == "turn":
            self._grants.append(_Grant(turn=request.turn, risk=request.risk, key=request.key))
        if not future.done():  # engine may have been cancelled in the same tick
            future.set_result(answer)

    def pending(self) -> tuple[ApprovalRequest, ...]:
        """Open requests, oldest first — for front ends driven by the event stream."""
        return tuple(request for request, _ in self._pending.values())

    # -- internals ------------------------------------------------------------

    def _resolve(self, request: ApprovalRequest, answer: ApprovalAnswer) -> ApprovalAnswer:
        # the single choke point every returned answer passes through, so the
        # audit trail cannot miss a resolution path (SAFETY §5)
        if self.audit is not None:
            self.audit.approval(
                request.turn, request.key or request.risk, answer.decision, answer.reason
            )
        return answer
