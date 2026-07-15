"""Handoff block roundtrip — the machine half of docs/PROTOCOLS.md."""

from pathlib import Path

from ironcore.memory.handoff import Handoff, append_handoff, latest_handoff, read_handoffs


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
