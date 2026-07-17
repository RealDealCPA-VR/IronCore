"""TurnEngine × OutcomeLedger (MS-8): the recording hooks, end to end.

Every case drives the real state machine with a scripted ``MockProvider`` and
the real default tool registry on a tmp workspace — zero network, zero model.
The pins: one tool-protocol sample per provider CALL at the ACTIVE rung, edit
samples only on REAL apply outcomes, verify + drift evidence, best-effort
sidecar persistence, ledger swap on ``repoint``, stale-counter reset on a
profile hot-swap — and ``outcomes=None`` (the default) as a strict no-op.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.core.events import TurnCompleted, TurnError
from ironcore.core.protocols import NoopVerifier, VerifyResult
from ironcore.core.roles import RoleRouter
from ironcore.envelope.outcomes import OutcomeLedger
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MalformedToolJSON, MockProvider, TimeoutFailure
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry

ORIGINAL = "def f():\n    return 1\n"
#: a SEARCH block whose lines do not exist in ORIGINAL — a mechanical patch failure.
MISSING_SR = "<<<<<<< SEARCH\n    return 9\n=======\n    return 2\n>>>>>>> REPLACE"


def _profile(protocol: str = "native", model: str = "mock") -> CapabilityProfile:
    tp: dict[str, float] = {"native": 1.0} if protocol == "native" else {}
    return CapabilityProfile(model_id=model, honest_context=8192, tool_protocols=tp)


def _text(content: str, calls: list[ToolCall] | None = None) -> CompletionResult:
    return CompletionResult(
        message=Message(role="assistant", content=content, tool_calls=calls or [])
    )


def _call(name: str, args: dict, cid: str = "c1") -> ToolCall:
    return ToolCall(id=cid, name=name, arguments=args)


def _ledger(tmp_path, model: str = "mock") -> OutcomeLedger:
    return OutcomeLedger.load(tmp_path / "env", model)


def _engine(
    tmp_path,
    script,
    *,
    outcomes: OutcomeLedger | None = None,
    mode: Mode = Mode.ACCEPT_EDITS,
    protocol: str = "native",
    verifier=None,
    roles: RoleRouter | None = None,
    settings: Settings | None = None,
    provider: MockProvider | None = None,
) -> TurnEngine:
    settings = settings if settings is not None else Settings()
    tools = build_default_registry(settings, tmp_path)
    return TurnEngine(
        provider if provider is not None else MockProvider(list(script)),
        tools,
        settings,
        _profile(protocol),
        mode,
        workspace=tmp_path,
        verifier=verifier if verifier is not None else NoopVerifier(),
        snapshots=None,
        roles=roles,
        outcomes=outcomes,
    )


def drive(engine: TurnEngine, user_input: str = "go") -> list:
    events: list = []

    async def _run():
        async for ev in engine.run_turn(user_input):
            events.append(ev)

    asyncio.run(_run())
    return events


class FailingVerifier:
    """Always red — drives the fed-back-once-then-goal-unmet path."""

    def __init__(self):
        self.calls = 0

    async def verify(self, workspace, settings, state, touched_files) -> VerifyResult:
        self.calls += 1
        return VerifyResult(ok=False, summary="2 tests failing", ran=["pytest -q"])


# --------------------------------------------------------------------------- #
# tool-protocol samples
# --------------------------------------------------------------------------- #


def test_native_turn_records_attempts_and_writes_the_sidecar(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    ledger = _ledger(tmp_path)
    engine = _engine(
        tmp_path,
        [_text("", [_call("read_file", {"path": "a.txt"})]), _text("done")],
        outcomes=ledger,
    )
    events = drive(engine)
    assert isinstance(events[-1], TurnCompleted) and events[-1].stop_reason == "done"
    counter = ledger.tool_protocols["native"]
    assert (counter.attempts, counter.failures) == (2, 0)  # one sample per CALL
    assert ledger.turns == 1 and ledger.drift_events == 0
    # best-effort persistence: the sidecar landed next to the envelope cache
    assert OutcomeLedger.path_for(tmp_path / "env", "mock").exists()
    assert OutcomeLedger.load(tmp_path / "env", "mock").tool_protocols["native"].attempts == 2


def test_malformed_stream_output_counts_a_native_failure(tmp_path: Path):
    ledger = _ledger(tmp_path)
    engine = _engine(
        tmp_path, [MalformedToolJSON(), _text("recovered; stopping")], outcomes=ledger
    )
    drive(engine)
    counter = ledger.tool_protocols["native"]
    assert (counter.attempts, counter.failures) == (2, 1)


def test_text_floor_records_text_protocol_failures(tmp_path: Path):
    ledger = _ledger(tmp_path)
    engine = _engine(
        tmp_path,
        [_text("```ironcall\n{broken\n```"), _text("nothing more to do")],
        outcomes=ledger,
        protocol="floor",
    )
    drive(engine)
    counter = ledger.tool_protocols["text_protocol"]
    assert (counter.attempts, counter.failures) == (2, 1)
    assert "native" not in ledger.tool_protocols  # evidence lands at the ACTIVE rung


def test_transport_failure_records_no_protocol_evidence(tmp_path: Path):
    ledger = _ledger(tmp_path)
    engine = _engine(tmp_path, [TimeoutFailure()], outcomes=ledger)
    events = drive(engine)
    assert isinstance(events[-1], TurnError)
    assert ledger.tool_protocols == {}  # a timeout says nothing about the protocol
    assert ledger.turns == 0  # a transport-fatal turn is not a coherence sample


# --------------------------------------------------------------------------- #
# edit-format samples
# --------------------------------------------------------------------------- #


def test_patch_failure_records_an_edit_format_failure(tmp_path: Path):
    (tmp_path / "app.py").write_text(ORIGINAL, encoding="utf-8")
    ledger = _ledger(tmp_path)
    engine = _engine(
        tmp_path,
        [
            _text(
                "",
                [_call("edit_file",
                       {"path": "app.py", "format": "search_replace", "edit": MISSING_SR})],
            ),
            _text("giving up"),
        ],
        outcomes=ledger,
    )
    drive(engine)
    counter = ledger.edit_formats["search_replace"]
    assert (counter.attempts, counter.failures) == (1, 1)


def test_successful_edit_records_an_edit_format_success(tmp_path: Path):
    (tmp_path / "app.py").write_text(ORIGINAL, encoding="utf-8")
    good = "<<<<<<< SEARCH\n    return 1\n=======\n    return 2\n>>>>>>> REPLACE"
    ledger = _ledger(tmp_path)
    engine = _engine(
        tmp_path,
        [
            _text(
                "",
                [_call("edit_file",
                       {"path": "app.py", "format": "search_replace", "edit": good})],
            ),
            _text("done"),
        ],
        outcomes=ledger,
    )
    drive(engine)
    counter = ledger.edit_formats["search_replace"]
    assert (counter.attempts, counter.failures) == (1, 0)
    assert "return 2" in (tmp_path / "app.py").read_text(encoding="utf-8")


def test_file_not_found_and_bad_args_never_poison_format_stats(tmp_path: Path):
    ledger = _ledger(tmp_path)
    engine = _engine(
        tmp_path,
        [
            _text(
                "",
                [
                    _call("edit_file",
                          {"path": "missing.py", "format": "search_replace",
                           "edit": MISSING_SR}, "e1"),
                    _call("edit_file",
                          {"path": "missing.py", "format": "not-a-format",
                           "edit": "x"}, "e2"),
                ],
            ),
            _text("ok"),
        ],
        outcomes=ledger,
    )
    drive(engine)
    assert ledger.edit_formats == {}  # neither outcome says anything about the FORMAT


# --------------------------------------------------------------------------- #
# verify + drift
# --------------------------------------------------------------------------- #


def test_verify_failures_and_drift_are_recorded(tmp_path: Path):
    ledger = _ledger(tmp_path)
    engine = _engine(
        tmp_path,
        [
            _text("", [_call("write_file", {"path": "x.txt", "content": "hi"})]),
            _text("done i think"),
            _text("still done"),
        ],
        outcomes=ledger,
        verifier=FailingVerifier(),
    )
    events = drive(engine)
    assert events[-1].stop_reason == "goal-unmet"
    assert (ledger.verify_runs, ledger.verify_failures) == (2, 2)
    assert (ledger.turns, ledger.drift_events) == (1, 1)


# --------------------------------------------------------------------------- #
# no-op default, hot-swap reset, repoint ledger swap, role routing
# --------------------------------------------------------------------------- #


def test_outcomes_none_is_a_strict_noop(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    script = [_text("", [_call("read_file", {"path": "a.txt"})]), _text("done")]
    wired = _engine(tmp_path, script, outcomes=_ledger(tmp_path))
    bare_ws = tmp_path / "bare"
    bare_ws.mkdir()
    (bare_ws / "a.txt").write_text("hello", encoding="utf-8")
    bare = _engine(bare_ws, script)  # outcomes defaults to None
    wired_events = [type(e).__name__ for e in drive(wired)]
    bare_events = [type(e).__name__ for e in drive(bare)]
    assert wired_events == bare_events  # identical event sequence
    assert not list(bare_ws.rglob("*.outcomes.json"))  # ... and no sidecar anywhere


def test_profile_hot_swap_resets_stale_counters(tmp_path: Path):
    ledger = _ledger(tmp_path)
    engine = _engine(
        tmp_path,
        [_text("floor turn"), _text("native turn")],
        outcomes=ledger,
        protocol="floor",
    )
    drive(engine)
    assert ledger.tool_protocols["text_protocol"].attempts == 1
    # a background probe lands: new generation -> stale floor evidence must go
    engine.profile = CapabilityProfile(
        model_id="mock", source="probed", probed_at="2026-07-17T00:00:00+00:00",
        tool_protocols={"native": 1.0},
    )
    drive(engine, "again")
    assert "text_protocol" not in ledger.tool_protocols  # reset by ensure_stamp
    assert ledger.tool_protocols["native"].attempts == 1
    assert ledger.turns == 1  # turn counters reset with the generation too


def test_repoint_swaps_the_ledger_to_the_new_models_sidecar(tmp_path: Path):
    ledger = _ledger(tmp_path)
    engine = _engine(tmp_path, [_text("first model")], outcomes=ledger)
    drive(engine)
    assert engine.outcomes is ledger and ledger.turns == 1
    engine.repoint(MockProvider([_text("second model")]),
                   CapabilityProfile(model_id="other", tool_protocols={"native": 1.0}))
    drive(engine, "again")
    # the engine now records into OTHER's ledger, loaded from the same dir
    assert engine.outcomes is not ledger
    assert engine.outcomes.model_id == "other"
    assert engine.outcomes.turns == 1
    # ... and both sidecars persisted independently
    assert OutcomeLedger.load(tmp_path / "env", "mock").turns == 1
    assert OutcomeLedger.load(tmp_path / "env", "other").turns == 1


def test_routed_role_turns_record_nothing(tmp_path: Path):
    # v1 rule: evidence is recorded ONLY when the loop role resolved to the
    # PRIMARY pair — a routed coder runs a different model whose outcomes must
    # not tune the primary model's ladders.
    settings = Settings.model_validate({"roles": {"coder": "tiny-7b"}})
    router = RoleRouter(
        settings,
        providers={"coder": MockProvider([_text("routed reply")])},
        profiles={"tiny-7b": CapabilityProfile(model_id="tiny-7b")},
    )
    ledger = _ledger(tmp_path)
    engine = _engine(tmp_path, [], outcomes=ledger, roles=router, settings=settings)
    events = drive(engine)
    assert isinstance(events[-1], TurnCompleted)
    assert ledger.tool_protocols == {} and ledger.turns == 0
    assert not (tmp_path / "env").exists()  # nothing was ever saved
