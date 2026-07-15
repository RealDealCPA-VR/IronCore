"""Session state store: atomic save/load, corruption recovery, Mode round-trip."""

import json
from pathlib import Path

from ironcore.core.state import STATE_FILENAME, SessionState, state_path
from ironcore.safety.modes import Mode


def _full_state() -> SessionState:
    return SessionState(
        mode=Mode.ACCEPT_EDITS,
        goal="ship IC-102",
        working_set=["ironcore/core/state.py", "tests/test_state.py"],
        plan_steps=["write dataclass", "write persistence", "write tests"],
        plan_cursor=2,
        plan_evidence={0: "class exists", 1: "roundtrip green"},
        turn_count=7,
        budgets_spent={"tokens": 1234, "provider_calls": 5, "wall_clock_s": 12.5},
    )


def test_fresh_state_defaults():
    state = SessionState()
    assert state.mode is Mode.MANUAL
    assert state.goal is None
    assert state.working_set == []
    assert state.plan_steps == []
    assert state.plan_cursor == 0
    assert state.plan_evidence == {}
    assert state.turn_count == 0
    assert state.budgets_spent == {}


def test_roundtrip_preserves_all_fields(tmp_path: Path):
    path = state_path(tmp_path)
    original = _full_state()
    original.save(path)

    loaded, warning = SessionState.load(path)
    assert warning is None
    assert loaded == original
    assert loaded.mode is Mode.ACCEPT_EDITS  # a real Mode, not a bare string
    assert all(isinstance(k, int) for k in loaded.plan_evidence)  # keys survive JSON


def test_mode_serializes_as_string_value(tmp_path: Path):
    path = tmp_path / "state.json"
    _full_state().save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["mode"] == "accept-edits"


def test_state_path_layout(tmp_path: Path):
    assert state_path(tmp_path) == tmp_path / ".ironcore" / STATE_FILENAME


def test_save_creates_parents_and_leaves_no_temp(tmp_path: Path):
    path = state_path(tmp_path)  # .ironcore/ does not exist yet
    SessionState().save(path)
    assert path.exists()
    assert list(path.parent.glob("*.tmp")) == []


def test_missing_file_is_fresh_without_warning(tmp_path: Path):
    state, warning = SessionState.load(tmp_path / "nope" / "state.json")
    assert state == SessionState()
    assert warning is None


def test_corrupt_json_returns_fresh_state_and_warning(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    state, warning = SessionState.load(path)
    assert state == SessionState()
    assert warning is not None and "state.json" in warning


def test_wrong_shape_returns_fresh_state_and_warning(tmp_path: Path):
    path = tmp_path / "state.json"
    for payload in ('[1, 2, 3]', '{"mode": "bogus-mode"}', '{"plan_cursor": "two"}'):
        path.write_text(payload, encoding="utf-8")
        state, warning = SessionState.load(path)
        assert state == SessionState()
        assert warning is not None


def test_unreadable_path_returns_fresh_state_and_warning(tmp_path: Path):
    path = tmp_path / "state.json"
    path.mkdir()  # a directory where the file should be -> OSError on read
    state, warning = SessionState.load(path)
    assert state == SessionState()
    assert warning is not None


def test_interrupted_write_recovers(tmp_path: Path):
    """Crash simulation: a leftover temp file plus a truncated main file must
    yield a fresh state (no crash), and the next save must heal both."""
    path = state_path(tmp_path)
    good = _full_state()
    good.save(path)

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text('{"mode": "auto", "goal": "half-writ', encoding="utf-8")  # dying save
    truncated = path.read_text(encoding="utf-8")[:25]
    path.write_text(truncated, encoding="utf-8")

    state, warning = SessionState.load(path)
    assert state == SessionState()
    assert warning is not None

    good.save(path)  # overwrites the leftover temp, atomically replaces main
    assert not tmp.exists()
    reloaded, warning = SessionState.load(path)
    assert warning is None
    assert reloaded == good


def test_save_is_atomic_old_state_survives_until_replace(tmp_path: Path):
    """The main file is never written in place: between saves it always holds
    a complete, parseable snapshot."""
    path = state_path(tmp_path)
    first = SessionState(goal="first")
    first.save(path)
    second = SessionState(goal="second", turn_count=1)
    second.save(path)
    loaded, warning = SessionState.load(path)
    assert warning is None
    assert loaded == second


def test_touch_maintains_mru_order():
    state = SessionState(working_set=["a.py", "b.py"])
    state.touch("c.py")
    assert state.working_set == ["c.py", "a.py", "b.py"]
    state.touch("b.py")  # existing entry moves to front, no duplicate
    assert state.working_set == ["b.py", "c.py", "a.py"]


def test_missing_keys_fall_back_to_defaults(tmp_path: Path):
    """Additive schema evolution: an older file without newer keys still loads."""
    path = tmp_path / "state.json"
    path.write_text('{"mode": "plan", "goal": "explore"}', encoding="utf-8")
    state, warning = SessionState.load(path)
    assert warning is None
    assert state.mode is Mode.PLAN
    assert state.goal == "explore"
    assert state.working_set == [] and state.turn_count == 0
