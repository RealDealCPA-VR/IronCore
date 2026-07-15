"""Session store: append-only JSONL transcripts, corrupt-tolerant, resume-ready."""

import json
import logging
from pathlib import Path

import pytest

from ironcore.memory.sessions import (
    DEFAULT_MAX_SESSIONS,
    SessionRecord,
    SessionStore,
    sessions_dir,
)
from ironcore.providers.base import Message


def _store(tmp_path: Path, **kwargs) -> SessionStore:
    return SessionStore(tmp_path, **kwargs)


def test_default_cap_is_bounded():
    assert DEFAULT_MAX_SESSIONS == 200


def test_create_writes_header_and_returns_record(tmp_path: Path):
    store = _store(tmp_path)
    rec = store.create("s1", "2026-07-16T10:00:00+00:00", first_prompt="fix the bug")

    assert isinstance(rec, SessionRecord)
    assert rec.id == "s1"
    assert rec.created_at == "2026-07-16T10:00:00+00:00"
    assert rec.turn_count == 0
    assert rec.first_prompt == "fix the bug"
    assert rec.path == sessions_dir(tmp_path) / "s1.jsonl"
    assert rec.path.exists()

    header = json.loads(rec.path.read_text(encoding="utf-8").splitlines()[0])
    assert header == {
        "kind": "header",
        "v": 1,
        "id": "s1",
        "created_at": "2026-07-16T10:00:00+00:00",
        "first_prompt": "fix the bug",
    }


def test_append_then_load_returns_lines_in_order(tmp_path: Path):
    store = _store(tmp_path)
    store.create("s1", "2026-07-16T10:00:00+00:00", first_prompt="hi")
    store.append_user("s1", "add a function")
    store.append_assistant("s1", "done, tests pass")
    store.append_event("s1", {"type": "ToolCallFinished", "tool": "write_file"})

    lines = store.load("s1")
    assert lines[0]["kind"] == "header"
    body = [ln for ln in lines if ln["kind"] != "header"]
    assert body == [
        {"kind": "user", "text": "add a function"},
        {"kind": "assistant", "text": "done, tests pass"},
        {"kind": "event", "payload": {"type": "ToolCallFinished", "tool": "write_file"}},
    ]


def test_load_missing_session_returns_empty(tmp_path: Path):
    store = _store(tmp_path)
    assert store.load("nope") == []


def test_list_empty_workspace(tmp_path: Path):
    assert _store(tmp_path).list_sessions() == []


def test_list_sessions_newest_first_with_labels(tmp_path: Path):
    store = _store(tmp_path)
    store.create("old", "2026-07-16T09:00:00+00:00", first_prompt="old prompt")
    store.create("mid", "2026-07-16T10:00:00+00:00", first_prompt="mid prompt")
    store.create("new", "2026-07-16T11:00:00+00:00", first_prompt="new prompt")
    store.append_user("new", "one")
    store.append_user("new", "two")

    records = store.list_sessions()
    assert [r.id for r in records] == ["new", "mid", "old"]
    assert [r.first_prompt for r in records] == ["new prompt", "mid prompt", "old prompt"]
    assert records[0].turn_count == 2  # two user turns recorded
    assert records[1].turn_count == 0


def test_corrupt_line_skipped_on_load(tmp_path: Path, caplog):
    store = _store(tmp_path)
    rec = store.create("s1", "2026-07-16T10:00:00+00:00")
    store.append_user("s1", "good line")
    with rec.path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("{ this is not json\n")  # a torn / corrupt line
    store.append_assistant("s1", "after corrupt")

    with caplog.at_level(logging.WARNING):
        lines = store.load("s1")

    assert [ln["kind"] for ln in lines] == ["header", "user", "assistant"]
    assert "corrupt line" in caplog.text  # the skip was reported


def test_corrupt_header_file_skipped_by_list(tmp_path: Path):
    store = _store(tmp_path)
    store.create("good", "2026-07-16T10:00:00+00:00", first_prompt="ok")
    bad = sessions_dir(tmp_path) / "bad.jsonl"
    bad.write_text('not json at all\n{"kind": "user", "text": "x"}\n', encoding="utf-8")

    assert [r.id for r in store.list_sessions()] == ["good"]


def test_file_with_non_header_first_line_skipped(tmp_path: Path):
    store = _store(tmp_path)
    store.create("good", "2026-07-16T10:00:00+00:00")
    orphan = sessions_dir(tmp_path) / "orphan.jsonl"
    orphan.write_text(json.dumps({"kind": "user", "text": "no header"}) + "\n", encoding="utf-8")

    assert [r.id for r in store.list_sessions()] == ["good"]


def test_rehydrate_reconstructs_messages_and_tail(tmp_path: Path):
    store = _store(tmp_path)
    store.create("s1", "2026-07-16T10:00:00+00:00", first_prompt="start")
    store.append_user("s1", "write hello world")
    store.append_event("s1", {"type": "ToolCallRequested"})  # not a conversation message
    store.append_assistant("s1", "here is hello world")

    messages, summary = store.rehydrate("s1")
    assert messages == [
        Message(role="user", content="write hello world"),
        Message(role="assistant", content="here is hello world"),
    ]
    assert "hello world" in summary
    assert "2 message" in summary


def test_rehydrate_empty_session(tmp_path: Path):
    store = _store(tmp_path)
    store.create("s1", "2026-07-16T10:00:00+00:00")
    messages, summary = store.rehydrate("s1")
    assert messages == []
    assert "empty session" in summary


def test_rehydrate_long_tail_is_truncated(tmp_path: Path):
    store = _store(tmp_path)
    store.create("s1", "2026-07-16T10:00:00+00:00")
    store.append_user("s1", "x" * 500)  # long content must be previewed, not dumped whole
    _, summary = store.rehydrate("s1")
    assert "…" in summary
    assert len(summary) < 500


def test_prune_removes_oldest_beyond_cap(tmp_path: Path):
    store = _store(tmp_path, max_sessions=2)
    a = store.create("a", "2026-07-16T01:00:00+00:00")
    b = store.create("b", "2026-07-16T02:00:00+00:00")
    c = store.create("c", "2026-07-16T03:00:00+00:00")  # create() prunes -> 'a' dropped

    assert not a.path.exists()
    assert b.path.exists() and c.path.exists()
    assert [r.id for r in store.list_sessions()] == ["c", "b"]


def test_prune_returns_count(tmp_path: Path):
    store = _store(tmp_path, max_sessions=5)  # high, so create() never prunes here
    for i in range(4):
        store.create(f"s{i}", f"2026-07-16T0{i}:00:00+00:00")

    store.max_sessions = 2  # tighten, then prune explicitly
    assert store.prune() == 2
    assert len(store.list_sessions()) == 2


def test_prune_never_deletes_active_session(tmp_path: Path):
    store = _store(tmp_path, max_sessions=2)
    store.create("a", "2026-07-16T03:00:00+00:00")  # newest stamp
    store.create("b", "2026-07-16T02:00:00+00:00")
    c = store.create("c", "2026-07-16T01:00:00+00:00")  # oldest stamp, created last -> active

    # 'c' is the oldest by created_at but is the active session -> protected,
    # so the store deliberately keeps one file over the cap.
    assert c.path.exists()
    assert {r.id for r in store.list_sessions()} == {"a", "b", "c"}

    # once a newer session steals "active", 'c' becomes prunable.
    store.create("d", "2026-07-16T04:00:00+00:00")
    assert not c.path.exists()


def test_writes_use_lf_newlines(tmp_path: Path):
    store = _store(tmp_path)
    rec = store.create("s1", "2026-07-16T10:00:00+00:00")
    store.append_user("s1", "line")
    raw = rec.path.read_bytes()
    assert b"\r\n" not in raw  # never Windows CRLF, even on Windows
    assert raw.endswith(b"\n")


def test_reads_crlf_written_files(tmp_path: Path):
    store = _store(tmp_path)
    path = sessions_dir(tmp_path) / "s1.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            {
                "kind": "header",
                "id": "s1",
                "created_at": "2026-07-16T10:00:00+00:00",
                "first_prompt": "hi",
            }
        )
        + "\r\n"
        + json.dumps({"kind": "user", "text": "hello"})
        + "\r\n"
        + json.dumps({"kind": "assistant", "text": "world"})
        + "\r\n"
    )
    path.write_bytes(payload.encode("utf-8"))

    records = store.list_sessions()
    assert [r.id for r in records] == ["s1"]
    assert records[0].turn_count == 1
    messages, _ = store.rehydrate("s1")
    assert messages == [
        Message(role="user", content="hello"),
        Message(role="assistant", content="world"),
    ]


def test_append_event_tolerates_non_serializable_values(tmp_path: Path):
    store = _store(tmp_path)
    store.create("s1", "2026-07-16T10:00:00+00:00")
    store.append_event("s1", {"obj": {1, 2, 3}})  # a set is not JSON-native

    lines = store.load("s1")
    assert lines[-1]["kind"] == "event"
    assert isinstance(lines[-1]["payload"]["obj"], str)  # stringified via default=str


def test_invalid_session_id_rejected(tmp_path: Path):
    store = _store(tmp_path)
    for bad in ("", "a/b", "a\\b", "..", "../evil"):
        with pytest.raises(ValueError):
            store.create(bad, "2026-07-16T10:00:00+00:00")


def test_create_existing_session_raises(tmp_path: Path):
    store = _store(tmp_path)
    store.create("s1", "2026-07-16T10:00:00+00:00")
    with pytest.raises(ValueError):
        store.create("s1", "2026-07-16T11:00:00+00:00")
