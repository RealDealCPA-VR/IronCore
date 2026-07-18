"""Tests for the offline end-to-end demo (IC-1103, SPEC §14).

The demo drives the real TurnEngine against a scripted MockProvider, so these
prove the whole read → plan → edit → verify → done arc runs with no network and
no real model. Most tests call ``run_demo`` directly with a capture list (fast,
hermetic); two exercise the shipped entry points (``ironcore demo`` and
``python -m ironcore.demo``) via subprocess.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ironcore.demo.scenario import CHECK_FILENAME, GREETER_FILENAME, run_demo

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_capture(workspace: Path) -> tuple[int, str]:
    out: list[str] = []
    code = run_demo(workspace=workspace, emit=out.append)
    return code, "\n".join(out)


def test_run_demo_returns_zero_and_narrates_the_key_beats(tmp_path):
    code, text = _run_capture(tmp_path)

    assert code == 0
    # a tool card for the edit + the applied change (edit_file result line).
    # The card header is `<name>  <RISK CHIP>  <gate>`, matching the TUI's tool
    # cards; pinning the chip alongside the name is strictly narrower than the
    # old "tool: edit_file" (it also proves the WRITE risk is surfaced).
    assert "edit_file   WRITE " in text
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
        "read_file  READ",
        "edit_file   WRITE ",
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


def test_python_dash_m_ironcore_demo_exits_zero():
    proc = subprocess.run(
        [sys.executable, "-m", "ironcore.demo"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "stop_reason: done" in proc.stdout


def test_demo_lives_inside_the_package_so_it_ships_in_the_wheel():
    """The regression: `demo/` was a top-level dir, the wheel packages only
    `ironcore`, so the documented `python -m demo` did not exist after a pip
    install. It has to be importable as a subpackage, and there must be no
    top-level `demo` package squatting that name."""
    import ironcore.demo

    assert Path(ironcore.demo.__file__).parent.parent.name == "ironcore"
    assert not (REPO_ROOT / "demo").exists()


def test_ironcore_demo_smoke_prints_one_pass_line(capsys):
    """`ironcore demo --smoke` is FIX-4's release gate: one line, exit 0."""
    from ironcore.cli import main

    assert main(["demo", "--smoke"]) == 0
    out = capsys.readouterr().out
    assert "demo: PASS" in out
    assert "stop_reason" not in out  # the narration is collapsed, not printed
    assert out.isascii()


def test_ironcore_demo_narrates_by_default(capsys):
    from ironcore.cli import main

    assert main(["demo"]) == 0
    assert "stop_reason: done" in capsys.readouterr().out
