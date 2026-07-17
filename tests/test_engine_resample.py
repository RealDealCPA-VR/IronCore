"""TurnEngine × best-of-N escape hatches (MS-4): full-turn integration.

Every case drives the real state machine with a scripted ``MockProvider`` and
the real default tool registry on a tmp workspace — zero network, zero model.

ORDER-COUPLED SCRIPTS: MockProvider serves ``stream`` (the engine's main loop)
and ``complete`` (resample candidates) from ONE pop-queue, in engine order —
so a script reads top-to-bottom as [main call, candidate, candidate, …, main
call]. Edit an entry's position only if you know which seam consumes it.
"""

from __future__ import annotations

import asyncio

from ironcore.config.settings import Settings
from ironcore.core.approvals import ApprovalAnswer, ApprovalBroker
from ironcore.core.engine import TurnEngine
from ironcore.core.events import (
    ApprovalRequired,
    ResampleProgress,
    TextDelta,
    ToolCallFinished,
    ToolCallRequested,
    TurnCompleted,
)
from ironcore.core.protocols import DefaultBudget, DefaultRepairPolicy, NoopVerifier
from ironcore.core.roles import RoleRouter
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MalformedToolJSON, MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry

# --------------------------------------------------------------------------- #
# helpers (test_engine.py pattern)
# --------------------------------------------------------------------------- #

ORIGINAL = "def f():\n    return 1\n"
GOOD_SR = "<<<<<<< SEARCH\n    return 1\n=======\n    return 2\n>>>>>>> REPLACE"
#: a hunk whose context does not exist in ORIGINAL — mechanically unappliable.
BAD_DIFF = "@@ -1,2 +1,2 @@\n def g():\n-    return 9\n+    return 2\n"


def _profile(protocol: str = "native", model: str = "mock", ctx: int = 8192) -> CapabilityProfile:
    tp: dict[str, float] = {"native": 1.0} if protocol == "native" else {}
    return CapabilityProfile(model_id=model, honest_context=ctx, tool_protocols=tp)


def _text(content: str, calls: list[ToolCall] | None = None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=calls or [])
    )


def _call(name: str, args: dict, cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def _bad_edit(cid: str = "bad1") -> ToolCall:
    return _call("edit_file", {"path": "app.py", "format": "unified_diff", "edit": BAD_DIFF}, cid)


def _good_edit(cid: str = "win1") -> ToolCall:
    return _call("edit_file", {"path": "app.py", "format": "search_replace", "edit": GOOD_SR}, cid)


def _engine(
    tmp_path,
    script,
    *,
    best_of_n: int | None = None,
    mode: Mode = Mode.ACCEPT_EDITS,
    protocol: str = "native",
    broker: ApprovalBroker | None = None,
    repair=None,
    budget=None,
    roles: RoleRouter | None = None,
    provider: MockProvider | None = None,
) -> TurnEngine:
    data: dict = {}
    if best_of_n is not None:
        data["engine"] = {"best_of_n": best_of_n}
    settings = Settings.model_validate(data)
    tools = build_default_registry(settings, tmp_path)
    return TurnEngine(
        provider if provider is not None else MockProvider(list(script)),
        tools,
        settings,
        _profile(protocol),
        mode,
        workspace=tmp_path,
        approvals=broker,
        repair=repair,
        verifier=NoopVerifier(),
        budget=budget,
        snapshots=None,
        roles=roles,
    )


def _sequenced_broker(answers: list[str]) -> ApprovalBroker:
    """Answers asks in order (then denies) — the engine awaits on_request inline."""
    broker = ApprovalBroker(timeout=5.0)
    queue = list(answers)

    async def _on_request(req):
        decision = queue.pop(0) if queue else "deny"
        broker.answer(req.id, ApprovalAnswer(decision=decision))

    broker.on_request = _on_request
    return broker


def drive(engine: TurnEngine, user_input: str = "fix app.py") -> list:
    events: list = []

    async def _run():
        async for ev in engine.run_turn(user_input):
            events.append(ev)

    asyncio.run(_run())
    return events


def _of(events, cls) -> list:
    return [e for e in events if isinstance(e, cls)]


def _text_of(events) -> str:
    return "".join(e.text for e in _of(events, TextDelta))


def _write_app(tmp_path) -> None:
    (tmp_path / "app.py").write_text(ORIGINAL, encoding="utf-8")


# --------------------------------------------------------------------------- #
# (1) edit seam: a failed patch is rescued by a raced candidate, on disk
# --------------------------------------------------------------------------- #


def test_edit_seam_rescues_a_failed_patch_through_the_real_gate(tmp_path):
    _write_app(tmp_path)
    script = [
        _text("", [_bad_edit()]),  # stream: the model's unappliable diff
        _text("WINNER-OK", [_good_edit()]),  # complete: the raced candidate
        _text("done"),  # stream: the model stops
    ]
    engine = _engine(tmp_path, script, best_of_n=2)
    events = drive(engine)

    # the candidate's edit landed on disk, through the real tool
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "def f():\n    return 2\n"
    rp = _of(events, ResampleProgress)
    assert len(rp) == 1
    assert (rp[0].seam, rp[0].attempt, rp[0].total) == ("edit", 1, 1)
    # the resample happened BETWEEN the failed finish and the winner's request
    reqs = _of(events, ToolCallRequested)
    fins = _of(events, ToolCallFinished)
    assert len(reqs) == 2 and len(fins) == 2
    assert not fins[0].result.ok and fins[1].result.ok
    assert events.index(fins[0]) < events.index(rp[0]) < events.index(reqs[1])
    # the winner passed the real gate (ACCEPT_EDITS allows writes)
    assert reqs[1].decision == "allow" and reqs[1].risk == "write"
    assert _of(events, TurnCompleted)[0].stop_reason == "done"
    # the candidate was sampled in the EDIT band with one retry bump: 0.2 + 0.2
    provider = engine.provider
    assert provider.sampling_calls[1].temperature == 0.4
    # only the winner's assistant message entered history
    assert sum("WINNER-OK" in m.content for m in engine._conversation) == 1


def test_edit_seam_rescue_works_on_the_ironcall_text_floor(tmp_path):
    import json

    _write_app(tmp_path)

    def block(call: ToolCall) -> str:
        body = json.dumps({"tool": call.name, "args": call.arguments})
        return f"```ironcall\n{body}\n```"

    script = [
        _text(block(_bad_edit())),  # stream: unappliable diff, in-band
        _text(block(_good_edit())),  # complete: the raced candidate, in-band
        _text("all fixed"),  # stream: prose, no block -> stop
    ]
    events = drive(_engine(tmp_path, script, best_of_n=2, protocol="text_protocol"))

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "def f():\n    return 2\n"
    assert [e.seam for e in _of(events, ResampleProgress)] == ["edit"]
    assert _of(events, TurnCompleted)[0].stop_reason == "done"


# --------------------------------------------------------------------------- #
# (2) edit seam: exhaustion behaves exactly like the status quo
# --------------------------------------------------------------------------- #


def test_edit_seam_exhaustion_leaves_the_file_untouched_and_history_clean(tmp_path):
    _write_app(tmp_path)
    script = [
        _text("", [_bad_edit()]),  # stream: unappliable diff
        _text("LOSER-XYZ", [_bad_edit("bad2")]),  # complete: the candidate fails too
        _text("giving up"),  # stream: the model stops
    ]
    engine = _engine(tmp_path, script, best_of_n=2)
    events = drive(engine)

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == ORIGINAL  # byte-unchanged
    assert len(_of(events, ResampleProgress)) == 1
    assert "[resample] no candidate patch applied" in _text_of(events)
    assert len(_of(events, ToolCallFinished)) == 1  # no loser ever executed
    assert _of(events, TurnCompleted)[0].stop_reason == "done"
    # losers never pollute the conversation
    assert not any("LOSER-XYZ" in m.content for m in engine._conversation)


# --------------------------------------------------------------------------- #
# (3) parse seam: a GIVE_UP is rescued into a completed turn
# --------------------------------------------------------------------------- #


def test_parse_seam_rescues_a_give_up_into_tool_execution(tmp_path):
    (tmp_path / "hello.txt").write_text("hi there\n", encoding="utf-8")
    script = [
        MalformedToolJSON(),  # stream: repairable garbage
        _text("", [_call("read_file", {"path": "hello.txt"})]),  # complete: clean candidate
        _text("done"),  # stream: the model stops
    ]
    # max_retries=0 forces an immediate GIVE_UP -> the resample race is the rescue
    engine = _engine(
        tmp_path, script, best_of_n=2, mode=Mode.MANUAL, repair=DefaultRepairPolicy(max_retries=0)
    )
    events = drive(engine)

    rp = _of(events, ResampleProgress)
    assert [e.seam for e in rp] == ["parse"]
    fins = _of(events, ToolCallFinished)
    assert [f.call.name for f in fins] == ["read_file"] and fins[0].result.ok
    assert _of(events, TurnCompleted)[0].stop_reason == "done"  # NOT "error"


def test_parse_seam_exhaustion_still_ends_with_stop_reason_error(tmp_path):
    script = [
        MalformedToolJSON(),  # stream: repairable garbage
        MalformedToolJSON(),  # complete: the candidate is garbage too
    ]
    engine = _engine(
        tmp_path, script, best_of_n=2, mode=Mode.MANUAL, repair=DefaultRepairPolicy(max_retries=0)
    )
    events = drive(engine)

    assert len(_of(events, ResampleProgress)) == 1
    assert "[resample] no candidate parsed cleanly" in _text_of(events)
    assert not _of(events, ToolCallFinished)
    assert _of(events, TurnCompleted)[0].stop_reason == "error"  # exactly as today


# --------------------------------------------------------------------------- #
# (4) default off: best_of_n unset makes ZERO extra provider calls
# --------------------------------------------------------------------------- #


def test_default_config_never_resamples(tmp_path):
    _write_app(tmp_path)
    script = [
        _text("", [_bad_edit()]),  # stream: unappliable diff
        _text("ok, stopping"),  # stream: the model stops
    ]
    engine = _engine(tmp_path, script)  # best_of_n defaults to 1 = disabled
    events = drive(engine)

    assert engine.settings.engine.best_of_n == 1
    assert not _of(events, ResampleProgress)
    assert "[resample]" not in _text_of(events)
    assert len(engine.provider.calls) == 2  # the two scripted main-loop calls, nothing more
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == ORIGINAL
    assert _of(events, TurnCompleted)[0].stop_reason == "done"


# --------------------------------------------------------------------------- #
# (5) budget: should_continue stops the race early, candidates are charged
# --------------------------------------------------------------------------- #


def test_budget_bounds_the_race_before_n_minus_one_candidates(tmp_path):
    _write_app(tmp_path)
    script = [
        _text("", [_bad_edit()]),  # stream: unappliable diff (call 1)
        _text("", [_bad_edit("bad2")]),  # complete: candidate 1, fails (call 2 = the cap)
        # NO third entry: the budget must stop the race before candidate 2
    ]
    engine = _engine(
        tmp_path, script, best_of_n=3, budget=DefaultBudget(max_provider_calls=2)
    )
    events = drive(engine)

    # best_of_n=3 allows 2 candidates, but the budget allowed only 1
    assert len(_of(events, ResampleProgress)) == 1
    assert _of(events, TurnCompleted)[0].stop_reason == "budget"
    assert engine.provider.script == []  # both scripted calls were consumed, no more


# --------------------------------------------------------------------------- #
# (6) no gate bypass: a raced winner still ASKs in MANUAL and can be denied
# --------------------------------------------------------------------------- #


def test_manual_mode_still_gates_the_winning_candidate(tmp_path):
    _write_app(tmp_path)
    script = [
        _text("", [_bad_edit()]),  # stream: approved, runs, fails mechanically
        _text("", [_good_edit()]),  # complete: the winner — must still ASK
        _text("stopping"),  # stream: the model stops
    ]
    broker = _sequenced_broker(["approve", "deny"])  # approve the original, deny the winner
    events = drive(_engine(tmp_path, script, best_of_n=2, mode=Mode.MANUAL, broker=broker))

    asks = _of(events, ApprovalRequired)
    assert len(asks) == 2  # the winner went through the real ASK gate
    assert asks[1].call.arguments["edit"] == GOOD_SR
    fins = _of(events, ToolCallFinished)
    assert len(fins) == 1 and not fins[0].result.ok  # the denied winner never executed
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == ORIGINAL
    assert _of(events, TurnCompleted)[0].stop_reason == "done"


# --------------------------------------------------------------------------- #
# (7) MS-3 interplay: candidates are generated against the ACTIVE role binding
# --------------------------------------------------------------------------- #


def test_candidates_race_on_the_routed_coder_not_the_primary(tmp_path):
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
    primary = MockProvider()  # must receive NOTHING
    coder = MockProvider([
        MalformedToolJSON(),  # stream: the coder's garbage
        _text("", [_call("read_file", {"path": "hello.txt"})]),  # complete: its candidate
        _text("done"),
    ])
    settings = Settings.model_validate({"engine": {"best_of_n": 2}, "roles": {"coder": "tiny"}})
    router = RoleRouter(
        settings,
        providers={"coder": coder},
        profiles={"tiny": _profile(model="tiny", ctx=2048)},
    )
    tools = build_default_registry(settings, tmp_path)
    engine = TurnEngine(
        primary,
        tools,
        settings,
        _profile(model="big", ctx=8192),
        Mode.MANUAL,
        workspace=tmp_path,
        repair=DefaultRepairPolicy(max_retries=0),
        verifier=NoopVerifier(),
        snapshots=None,
        roles=router,
    )
    events = drive(engine)

    assert primary.calls == []  # the race ran on the ACTIVE (coder) binding
    assert len(coder.calls) == 3
    # the candidate call was sized by the CODER's window: 15% of 2048 = 307
    assert coder.sampling_calls[1].max_tokens == 307
    assert [e.seam for e in _of(events, ResampleProgress)] == ["parse"]
    assert _of(events, TurnCompleted)[0].stop_reason == "done"
