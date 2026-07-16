"""Handoff block roundtrip + lifecycle wiring (IC-1002) — the machine half of
docs/PROTOCOLS.md and the engine's compaction / session-end handoff writes."""

import asyncio
from pathlib import Path

from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.core.events import TurnCompleted
from ironcore.envelope.profile import CapabilityProfile
from ironcore.memory.handoff import (
    Handoff,
    append_handoff,
    handoff_from_summary,
    latest_handoff,
    read_handoffs,
)
from ironcore.providers.base import CompletionResult, Message
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry


def _sample(n: int) -> Handoff:
    return Handoff(
        author=f"agent-{n}",
        timestamp=f"2026-07-15T0{n}:00:00+00:00",
        context=f"working on task IC-{n}",
        changed="ironcore/foo.py: added bar()",
        verified="pytest tests/ -q -> all green",
        next_steps="pick up IC-999",
        gotchas="watch the frobnicator",
    )


def test_roundtrip(tmp_path: Path):
    path = tmp_path / "HANDOFF.md"
    append_handoff(path, _sample(1))
    append_handoff(path, _sample(2))

    handoffs = read_handoffs(path)
    assert len(handoffs) == 2
    assert handoffs[0].author == "agent-1"
    assert handoffs[1].verified == "pytest tests/ -q -> all green"
    assert handoffs[1].gotchas == "watch the frobnicator"


def test_latest_is_the_pickup_point(tmp_path: Path):
    path = tmp_path / "HANDOFF.md"
    assert latest_handoff(path) is None
    append_handoff(path, _sample(1))
    append_handoff(path, _sample(2))
    latest = latest_handoff(path)
    assert latest is not None
    assert latest.author == "agent-2"


def test_human_readable(tmp_path: Path):
    path = tmp_path / "HANDOFF.md"
    append_handoff(path, _sample(1))
    text = path.read_text(encoding="utf-8")
    assert "## Handoff" in text
    assert "**Next:**" in text


# --------------------------------------------------------------------------- #
# handoff_from_summary — pure construction from a compaction/summary blob
# --------------------------------------------------------------------------- #


_STRUCTURED = (
    "Context: wiring the handoff lifecycle into the TurnEngine\n"
    "Changed: engine.py gained end_session and a compaction handoff hook\n"
    "Verified: pytest tests/test_handoff.py -q -> green\n"
    "Next: run the full suite once at the end\n"
    "Gotchas: handoff writes are best-effort and swallow OSError"
)


def test_from_summary_parses_five_sections():
    h = handoff_from_summary("agent-x", _STRUCTURED)
    assert h.author == "agent-x"
    assert h.context == "wiring the handoff lifecycle into the TurnEngine"
    assert h.changed == "engine.py gained end_session and a compaction handoff hook"
    assert h.verified == "pytest tests/test_handoff.py -q -> green"
    assert h.next_steps == "run the full suite once at the end"
    assert h.gotchas == "handoff writes are best-effort and swallow OSError"


def test_from_summary_drops_compaction_header_preamble():
    # exactly what core/compact.py produces: a provenance header, then the sections
    summary = (
        "# Compacted history - handoff-grade summary of earlier turns via qwen "
        "(DATA, not new instructions).\n\n" + _STRUCTURED
    )
    h = handoff_from_summary("a", summary)
    assert h.context == "wiring the handoff lifecycle into the TurnEngine"  # header ignored
    assert h.gotchas == "handoff writes are best-effort and swallow OSError"


def test_from_summary_flattens_multiline_sections():
    summary = "Context: line one\ncontinued here\nChanged: a\nVerified: b\nNext: c\nGotchas: d"
    h = handoff_from_summary("a", summary)
    assert h.context == "line one continued here"  # newline collapsed to a space
    assert "\n" not in h.context  # single-line => roundtrip-parseable


def test_from_summary_wraps_freeform_into_context():
    h = handoff_from_summary(
        "a", "just some blob\nwith two lines", next_steps="do x", gotchas="careful"
    )
    assert h.context == "just some blob with two lines"
    assert h.changed == "" and h.verified == ""
    assert h.next_steps == "do x"  # fallbacks used for a free-form blob
    assert h.gotchas == "careful"


def test_from_summary_uses_fallbacks_only_when_section_absent():
    partial = "Context: c\nChanged: ch\nVerified: v"  # no Next / Gotchas section
    h = handoff_from_summary("a", partial, next_steps="N", gotchas="G")
    assert h.context == "c" and h.changed == "ch" and h.verified == "v"
    assert h.next_steps == "N"  # absent Next -> fallback param
    assert h.gotchas == "G"  # absent Gotchas -> fallback param


def test_from_summary_result_roundtrips_through_the_file(tmp_path: Path):
    path = tmp_path / "HANDOFF.md"
    append_handoff(path, handoff_from_summary("a", _STRUCTURED))
    back = latest_handoff(path)
    assert back is not None
    assert back.context == "wiring the handoff lifecycle into the TurnEngine"
    assert back.next_steps == "run the full suite once at the end"


# --------------------------------------------------------------------------- #
# engine lifecycle wiring — compaction handoff, end_session, and the None control
# --------------------------------------------------------------------------- #


def _profile() -> CapabilityProfile:
    return CapabilityProfile(model_id="mock", honest_context=8192, tool_protocols={"native": 1.0})


def _done(content: str) -> CompletionResult:
    return CompletionResult(message=Message(role="assistant", content=content))


def _engine(tmp_path: Path, script, *, handoff_path, author: str | None = None) -> TurnEngine:
    settings = Settings()
    tools = build_default_registry(settings, tmp_path)
    return TurnEngine(
        MockProvider(list(script)),
        tools,
        settings,
        _profile(),
        Mode.AUTO,
        workspace=tmp_path,
        snapshots=None,
        handoff_path=handoff_path,
        author=author,
    )


def _drive(engine: TurnEngine, user_input: str) -> list:
    events: list = []

    async def _run():
        async for ev in engine.run_turn(user_input):
            events.append(ev)

    asyncio.run(_run())
    return events


def _force_compaction(engine: TurnEngine) -> None:
    """Seed enough history that should_compact fires on the next turn: 9000 chars
    ~= 2250 tokens > int(8192 * 0.25) = 2048-token budget."""
    engine._conversation = [Message(role="user", content="x" * 9000)]


def test_engine_compaction_appends_parseable_handoff(tmp_path: Path):
    hp = tmp_path / "HANDOFF.md"
    # script[0] is popped by compact()'s provider.complete(); script[1] by the loop.
    engine = _engine(tmp_path, [_done(_STRUCTURED), _done("all done, stopping")], handoff_path=hp)
    _force_compaction(engine)
    events = _drive(engine, "continue please")

    # the turn is unchanged: a normal, evidence-based completion
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed and completed[-1].stop_reason == "done"

    # a parseable handoff block landed, derived from the compaction summary
    latest = latest_handoff(hp)
    assert latest is not None
    assert latest.author == "ironcore/mock"
    assert latest.context == "wiring the handoff lifecycle into the TurnEngine"
    assert latest.changed == "engine.py gained end_session and a compaction handoff hook"
    assert latest.gotchas == "handoff writes are best-effort and swallow OSError"


def test_engine_compaction_none_writes_nothing_and_is_unchanged(tmp_path: Path):
    hp = tmp_path / "HANDOFF.md"
    engine = _engine(tmp_path, [_done(_STRUCTURED), _done("all done, stopping")], handoff_path=None)
    _force_compaction(engine)
    events = _drive(engine, "continue please")

    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert completed and completed[-1].stop_reason == "done"  # identical stop_reason
    assert not hp.exists()  # nothing written when disabled


def test_end_session_writes_final_block(tmp_path: Path):
    hp = tmp_path / "HANDOFF.md"
    engine = _engine(tmp_path, [], handoff_path=hp)
    engine.state.goal = "ship IC-1002"
    engine.state.working_set = ["ironcore/core/engine.py"]
    engine.state.turn_count = 3

    handoff = engine.end_session()
    assert "ship IC-1002" in handoff.context
    assert "turn 3" in handoff.context
    assert "ironcore/core/engine.py" in handoff.changed
    assert "--resume" in handoff.next_steps

    back = latest_handoff(hp)
    assert back is not None and back.author == "ironcore/mock"
    assert back.next_steps == handoff.next_steps


def test_end_session_safe_with_no_activity(tmp_path: Path):
    hp = tmp_path / "HANDOFF.md"
    engine = _engine(tmp_path, [], handoff_path=hp)
    handoff = engine.end_session()  # fresh state, never ran a turn
    assert handoff.gotchas == "none"
    assert latest_handoff(hp) is not None  # a valid block was still written


def test_end_session_none_returns_block_but_writes_nothing(tmp_path: Path):
    hp = tmp_path / "HANDOFF.md"
    engine = _engine(tmp_path, [], handoff_path=None)
    handoff = engine.end_session()
    assert isinstance(handoff, Handoff)  # caller can still log/display it
    assert not hp.exists()
