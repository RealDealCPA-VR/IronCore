"""ApprovalBroker pins: approve/deny handshake, fail-closed timeout, turn-scoped grants.

No pytest-asyncio: each test drives one event loop via ``asyncio.run`` and
runs the engine side (``request()``) and the front-end side (``answer()``)
concurrently with ``asyncio.gather``.
"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from ironcore.core.approvals import ApprovalAnswer, ApprovalBroker, ApprovalRequest
from ironcore.safety.audit import AuditWriter


async def answer_when_pending(
    broker: ApprovalBroker,
    make_answer: Callable[[ApprovalRequest], ApprovalAnswer],
) -> ApprovalRequest:
    """Front-end stand-in: wait for the request to appear, then answer it."""
    while not broker.pending():
        await asyncio.sleep(0)
    request = broker.pending()[0]
    broker.answer(request.id, make_answer(request))
    return request


def read_approval_records(workspace: Path) -> list[dict]:
    files = sorted((workspace / ".ironcore" / "audit").glob("*.jsonl"))
    return [
        json.loads(line)
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


# --- accept criterion 1: grant/approve --------------------------------------


def test_approve_resolves_the_engine_side_request():
    async def go():
        broker = ApprovalBroker(timeout=5)
        broker.begin_turn(1)
        answer, request = await asyncio.gather(
            broker.request("--- a/x.py\n+++ b/x.py", risk="write", turn=1, key="write_file"),
            answer_when_pending(broker, lambda _r: ApprovalAnswer(decision="approve")),
        )
        assert answer.decision == "approve"
        assert answer.scope == "once"  # dataclass default
        assert answer.reason is None
        assert request.preview == "--- a/x.py\n+++ b/x.py"  # exact effect, not a paraphrase
        assert broker.pending() == ()  # resolved requests do not linger

    asyncio.run(go())


# --- accept criterion 2: deny with reason fed back ---------------------------


def test_deny_reason_is_fed_back_verbatim():
    async def go():
        broker = ApprovalBroker(timeout=5)
        broker.begin_turn(3)
        answer, _ = await asyncio.gather(
            broker.request("rm -rf build", risk="exec", turn=3, key="shell"),
            answer_when_pending(
                broker, lambda _r: ApprovalAnswer(decision="deny", reason="wrong directory")
            ),
        )
        assert answer.decision == "deny"
        assert answer.reason == "wrong directory"  # SAFETY §4: verbatim

    asyncio.run(go())


# --- accept criterion 3: timeout → auto-deny ---------------------------------


def test_unanswered_request_times_out_to_deny():
    async def go():
        broker = ApprovalBroker(timeout=0.02)
        broker.begin_turn(1)
        answer = await broker.request("GET https://example.com", risk="net", turn=1)
        assert answer.decision == "deny"  # fail closed
        assert "fail closed" in (answer.reason or "")
        assert broker.pending() == ()  # timed-out request cleaned up

    asyncio.run(go())


# --- accept criterion 4: turn-scoped grant + expiry ---------------------------


def test_turn_grant_auto_approves_within_turn_and_expires_after_it():
    prompts: list[ApprovalRequest] = []

    async def on_request(request: ApprovalRequest) -> None:
        prompts.append(request)

    async def go():
        broker = ApprovalBroker(timeout=5, on_request=on_request)
        broker.begin_turn(1)
        first, _ = await asyncio.gather(
            broker.request("diff A", risk="write", turn=1, key="write_file"),
            answer_when_pending(
                broker, lambda _r: ApprovalAnswer(decision="approve", scope="turn")
            ),
        )
        assert first.decision == "approve"
        assert len(prompts) == 1

        # same turn, matching risk/key: covered by the grant — no prompt
        second = await broker.request("diff B", risk="write", turn=1, key="write_file")
        assert second.decision == "approve"
        assert second.scope == "turn"
        assert len(prompts) == 1  # the front end was NOT signalled again

        broker.end_turn()
        broker.begin_turn(2)

        # fresh turn: the grant is gone (SAFETY §3) — the front end is prompted again
        third, _ = await asyncio.gather(
            broker.request("diff C", risk="write", turn=2, key="write_file"),
            answer_when_pending(broker, lambda _r: ApprovalAnswer(decision="deny")),
        )
        assert third.decision == "deny"
        assert len(prompts) == 2

    asyncio.run(go())


def test_grant_matching_is_scoped_to_risk_and_key():
    async def go():
        broker = ApprovalBroker(timeout=0.02)  # non-covered requests fail closed fast
        broker.begin_turn(1)
        granted, _ = await asyncio.gather(
            broker.request("diff", risk="write", turn=1),  # key=None → risk-wide grant
            answer_when_pending(
                broker, lambda _r: ApprovalAnswer(decision="approve", scope="turn")
            ),
        )
        assert granted.decision == "approve"
        # "approve all writes this turn": any write is covered, keyed or not
        keyed = await broker.request("d2", risk="write", turn=1, key="edit_file")
        assert keyed.decision == "approve"
        # a different RISK is not covered → parks pending → timeout deny
        other_risk = await broker.request("cmd", risk="exec", turn=1)
        assert other_risk.decision == "deny"

        broker.begin_turn(2)
        narrow, _ = await asyncio.gather(
            broker.request("cmd", risk="exec", turn=2, key="shell"),
            answer_when_pending(
                broker, lambda _r: ApprovalAnswer(decision="approve", scope="turn")
            ),
        )
        assert narrow.decision == "approve"
        # a keyed grant covers only the same key
        assert (await broker.request("c2", risk="exec", turn=2, key="shell")).decision == "approve"
        assert (await broker.request("f", risk="exec", turn=2, key="fetch")).decision == "deny"

    asyncio.run(go())


def test_once_scope_records_no_grant():
    async def go():
        broker = ApprovalBroker(timeout=0.02)
        broker.begin_turn(1)
        first, _ = await asyncio.gather(
            broker.request("diff", risk="write", turn=1, key="write_file"),
            answer_when_pending(broker, lambda _r: ApprovalAnswer(decision="approve")),
        )
        assert first.decision == "approve"
        # identical follow-up is NOT covered — unanswered, so it fail-closes
        second = await broker.request("diff2", risk="write", turn=1, key="write_file")
        assert second.decision == "deny"

    asyncio.run(go())


# --- front-end wiring shapes --------------------------------------------------


def test_on_request_callback_may_answer_inline():
    # the IC-703 shape: the callback itself resolves the request
    async def go():
        broker = ApprovalBroker(timeout=5)

        async def on_request(request: ApprovalRequest) -> None:
            broker.answer(request.id, ApprovalAnswer(decision="deny", reason="policy"))

        broker.on_request = on_request
        broker.begin_turn(1)
        answer = await broker.request("cmd", risk="exec", turn=1, key="shell")
        assert (answer.decision, answer.reason) == ("deny", "policy")

    asyncio.run(go())


def test_crashing_on_request_callback_denies_fail_closed():
    async def boom(_request: ApprovalRequest) -> None:
        raise RuntimeError("front end exploded")

    async def go():
        broker = ApprovalBroker(timeout=5, on_request=boom)
        broker.begin_turn(1)
        answer = await broker.request("cmd", risk="exec", turn=1)
        assert answer.decision == "deny"
        assert "front end exploded" in (answer.reason or "")
        assert broker.pending() == ()

    asyncio.run(go())


def test_answering_an_unknown_request_raises_key_error():
    broker = ApprovalBroker()
    with pytest.raises(KeyError, match="apr-999"):
        broker.answer("apr-999", ApprovalAnswer(decision="approve"))


# --- audit trail ---------------------------------------------------------------


def test_every_resolved_answer_is_audited(tmp_path):
    writer = AuditWriter(tmp_path, "sess-1")

    async def go():
        broker = ApprovalBroker(timeout=0.02, audit=writer)
        broker.begin_turn(4)
        await asyncio.gather(
            broker.request("diff", risk="write", turn=4, key="write_file"),
            answer_when_pending(
                broker, lambda _r: ApprovalAnswer(decision="approve", scope="turn")
            ),
        )
        await broker.request("diff2", risk="write", turn=4, key="write_file")  # grant auto-ok
        await broker.request("cmd", risk="exec", turn=4)  # timeout deny

    asyncio.run(go())
    records = read_approval_records(tmp_path)
    assert [r["event"] for r in records] == ["approval", "approval", "approval"]
    assert [r["answer"] for r in records] == ["approve", "approve", "deny"]
    assert [r["turn"] for r in records] == [4, 4, 4]
    assert records[0]["tool"] == "write_file"  # key stands in for the tool name
    assert records[2]["tool"] == "exec"  # no key → risk class, honestly labelled
    assert "grant" in records[1]["reason"]  # auto-approval says why
    assert "fail closed" in records[2]["reason"]  # timeout deny says why


def test_broker_without_audit_still_works(tmp_path):
    # decoupling pin: audit=None must not change behavior
    async def go():
        broker = ApprovalBroker(timeout=0.02, audit=None)
        broker.begin_turn(1)
        answer = await broker.request("diff", risk="write", turn=1)
        assert answer.decision == "deny"

    asyncio.run(go())
    assert not (tmp_path / ".ironcore").exists()
