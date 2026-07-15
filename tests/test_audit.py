"""Audit trail pins: parseable lines, capped previews, no rewrite verbs, date naming."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import ironcore.safety.audit as audit_module
from ironcore.safety import Decision, Mode
from ironcore.safety.audit import EVENT_TYPES, PREVIEW_MAX, AuditWriter, fingerprint_args

TS = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_writer(tmp_path: Path) -> AuditWriter:
    return AuditWriter(tmp_path, "sess-1")


def test_event_vocabulary_is_pinned():
    # IC-403/IC-502 write against these names — changing them is a contract change
    assert EVENT_TYPES == {"tool_call", "gate", "approval", "mode_change", "turn_end"}


def test_every_line_parses_as_json_with_core_fields(tmp_path):
    w = make_writer(tmp_path)
    w.tool_call(1, "read_file", {"path": "a.py"}, "ok", ts=TS)
    w.gate(1, "shell", {"cmd": "rm -rf /"}, Decision.DENY, ts=TS)
    w.approval(2, "write_file", "deny", "looks wrong", ts=TS)
    w.mode_change(2, Mode.MANUAL, Mode.AUTO, ts=TS)
    w.turn_end(2, "completed", ts=TS)
    records = read_records(w.path_for(TS))
    expected = ["tool_call", "gate", "approval", "mode_change", "turn_end"]
    assert [r["event"] for r in records] == expected
    for r in records:
        assert r["session"] == "sess-1"
        assert isinstance(r["turn"], int)
        assert datetime.fromisoformat(r["ts"]).utcoffset() == timedelta(0)  # ISO-8601 UTC


def test_preview_capped_for_10kb_blob(tmp_path):
    blob = "x" * 10_240
    w = make_writer(tmp_path)
    rec = w.tool_call(1, "shell", {"cmd": blob}, "ok", ts=TS)
    assert len(rec["args_preview"]) <= PREVIEW_MAX
    on_disk = read_records(w.path_for(TS))[0]
    assert len(on_disk["args_preview"]) <= PREVIEW_MAX
    assert blob not in w.path_for(TS).read_text(encoding="utf-8")  # full args never hit disk
    assert len(on_disk["args_sha256"]) == 64
    int(on_disk["args_sha256"], 16)  # sha256 hex


def test_no_rewrite_api_exists():
    forbidden = ("delete", "remove", "truncate", "rewrite")
    for name in dir(AuditWriter):
        assert not any(bad in name.lower() for bad in forbidden), name
    for name in dir(audit_module):
        assert not any(bad in name.lower() for bad in forbidden), name


def test_date_file_naming_from_injected_timestamp(tmp_path):
    w = make_writer(tmp_path)
    w.turn_end(0, ts=datetime(2027, 12, 31, 23, 59, 59, tzinfo=UTC))
    assert (tmp_path / ".ironcore" / "audit" / "2027-12-31.jsonl").exists()


def test_naive_timestamp_treated_as_utc_not_local(tmp_path):
    w = make_writer(tmp_path)
    rec = w.turn_end(0, ts=datetime(2026, 6, 1, 0, 30, 0))
    assert rec["ts"].startswith("2026-06-01T00:30:00")
    assert (tmp_path / ".ironcore" / "audit" / "2026-06-01.jsonl").exists()


def test_injected_clock_stamps_and_names_the_file(tmp_path):
    w = AuditWriter(tmp_path, "sess-1", clock=lambda: TS)
    rec = w.turn_end(3)
    assert rec["ts"] == TS.isoformat()
    assert (tmp_path / ".ironcore" / "audit" / "2026-01-02.jsonl").exists()


def test_append_only_across_writes(tmp_path):
    w = make_writer(tmp_path)
    w.turn_end(1, ts=TS)
    w.turn_end(2, ts=TS)
    assert [r["turn"] for r in read_records(w.path_for(TS))] == [1, 2]


def test_second_writer_appends_never_clobbers(tmp_path):
    AuditWriter(tmp_path, "sess-a").turn_end(1, ts=TS)
    w2 = AuditWriter(tmp_path, "sess-b")
    w2.turn_end(1, ts=TS)
    assert [r["session"] for r in read_records(w2.path_for(TS))] == ["sess-a", "sess-b"]


def test_gate_records_decision_tool_and_fingerprint(tmp_path):
    rec = make_writer(tmp_path).gate(4, "shell", {"cmd": "git status"}, Decision.ASK, ts=TS)
    assert rec["decision"] == "ask"
    assert rec["tool"] == "shell"
    assert "git status" in rec["args_preview"]


def test_tool_call_records_status(tmp_path):
    rec = make_writer(tmp_path).tool_call(1, "edit_file", {"path": "x"}, "error", ts=TS)
    assert rec["status"] == "error"
    assert rec["tool"] == "edit_file"


def test_approval_answer_and_optional_reason(tmp_path):
    w = make_writer(tmp_path)
    rec = w.approval(3, "shell", "deny", "not in the plan", ts=TS)
    assert (rec["answer"], rec["reason"]) == ("deny", "not in the plan")
    assert make_writer(tmp_path).approval(3, "shell", "approve", ts=TS)["reason"] is None


def test_mode_change_and_turn_end_fields(tmp_path):
    w = make_writer(tmp_path)
    mc = w.mode_change(5, Mode.MANUAL, Mode.ACCEPT_EDITS, ts=TS)
    te = w.turn_end(5, "budget", ts=TS)
    assert (mc["from_mode"], mc["to_mode"]) == ("manual", "accept-edits")
    assert te["stop_reason"] == "budget"


def test_args_hash_stable_across_key_order():
    d1, p1 = fingerprint_args({"a": 1, "b": 2})
    d2, p2 = fingerprint_args({"b": 2, "a": 1})
    assert (d1, p1) == (d2, p2)


def test_unknown_event_type_and_missing_turn_rejected(tmp_path):
    w = make_writer(tmp_path)
    with pytest.raises(ValueError):
        w.write({"event": "sneaky", "turn": 0})
    with pytest.raises(ValueError):
        w.write({"event": "gate"})  # no turn
