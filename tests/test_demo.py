"""Tests for the offline end-to-end demo (IC-1103, SPEC §14).

The demo drives the real TurnEngine against a scripted MockProvider, so these
prove the whole read → plan → edit → verify → done arc runs with no network and
no real model. Most tests call ``run_demo`` directly with a capture list (fast,
hermetic); one exercises the ``python -m demo`` entry point via subprocess.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from demo.scenario import CHECK_FILENAME, GREETER_FILENAME, run_demo

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_capture(workspace: Path) -> tuple[int, str]:
    out: list[str] = []
    code = run_demo(workspace=workspace, emit=out.append)
    return code, "\n".join(out)


def test_run_demo_returns_zero_and_narrates_the_key_beats(tmp_path):
    code, text = _run_capture(tmp_path)

    assert code == 0
    # a tool card for the edit + the applied change (edit_file result line)
    assert "tool: edit_file" in text
    assert "applied" in text  # "applied search_replace edit to greeter.py ..."
    # verification really ran and passed
    assert "verify passed" in text
    assert CHECK_FILENAME in text  # the actual verify command is shown
    # an honest, evidence-based completion
    assert "stop_reason: done" in text
    assert "demo complete" in text.lower()


def test_narration_shows_the_full_arc_in_order(tmp_path):
    _, text = _run_capture(tmp_path)
    # read happens before the edit, which happens before verify + completion
    order = [
        "tool: read_file",
        "tool: edit_file",
        "verify passed",
        "stop_reason: done",
    ]
    positions = [text.find(beat) for beat in order]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions), positions


def test_demo_actually_applies_the_edit_on_disk(tmp_path):
    code, _ = _run_capture(tmp_path)

    assert code == 0
    greeter = (tmp_path / GREETER_FILENAME).read_text(encoding="utf-8")
    assert 'return f"Hello, {name}!"' in greeter  # the '!' really landed on disk


def test_demo_writes_only_inside_its_workspace(tmp_path):
    ws = tmp_path / "ws"
    code, _ = _run_capture(ws)

    assert code == 0
    assert (ws / GREETER_FILENAME).exists()
    assert (ws / CHECK_FILENAME).exists()
    # nothing leaked into the parent dir — the only child is the workspace itself
    assert [p.name for p in tmp_path.iterdir()] == ["ws"]


def test_demo_is_idempotent_across_fresh_workspaces(tmp_path):
    first_code, first = _run_capture(tmp_path / "a")
    second_code, second = _run_capture(tmp_path / "b")

    assert first_code == 0 and second_code == 0

    def _strip_workspace_line(text: str) -> str:
        return "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("workspace")
        )

    # identical narration modulo the (path-dependent) workspace header line
    assert _strip_workspace_line(first) == _strip_workspace_line(second)


def test_run_demo_with_no_workspace_uses_a_throwaway_tempdir():
    out: list[str] = []
    code = run_demo(emit=out.append)  # workspace=None -> internal tempfile dir

    assert code == 0
    assert "stop_reason: done" in "\n".join(out)


def test_python_dash_m_demo_exits_zero():
    proc = subprocess.run(
        [sys.executable, "-m", "demo"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "stop_reason: done" in proc.stdout
