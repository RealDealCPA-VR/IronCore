"""WorkflowView (IC-904): the pure grouped-tree renderer + one Pilot mount test.

``render_progress`` owns the grouping contract, so it is asserted directly on
hand-built beat lists (no app). One headless Pilot test (``asyncio.run`` wrapping
``async with app.run_test()`` — the shell-test pattern) proves the widget mounts
and re-renders as beats arrive.
"""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual.app import App, ComposeResult

from ironcore.tui.widgets.workflowview import WorkflowView, render_progress
from ironcore.workflows.engine import WorkflowProgress


def _beat(kind, phase_id, *, detail="", index=None, total=None) -> WorkflowProgress:
    return WorkflowProgress(
        phase_id=phase_id, kind=kind, detail=detail, index=index, total=total
    )


def _full_run() -> list[WorkflowProgress]:
    """A complete two-phase run: fanout (one item fails) then reduce."""
    return [
        _beat("phase_start", "find", detail="fanout", index=1, total=2),
        _beat("item_done", "find", detail="ok", index=1, total=2),
        _beat("item_done", "find", detail="failed: boom", index=2, total=2),
        _beat("phase_done", "find", detail="2 result(s), 1 failed", index=1, total=2),
        _beat("phase_start", "report", detail="reduce", index=2, total=2),
        _beat("phase_done", "report", detail="3", index=2, total=2),
        _beat("workflow_done", "report", detail="ok"),
    ]


# --------------------------------------------------------------------------- #
# (1) pure render_progress: grouped tree with phase headers + item marks
# --------------------------------------------------------------------------- #


def test_render_progress_groups_phases_and_marks_items():
    text = render_progress(_full_run())
    assert isinstance(text, Text)
    plain = text.plain

    # workflow-level status line
    assert "workflow" in plain and "done" in plain
    # a header per phase, kind-tagged and grouped
    assert "find [fanout]" in plain
    assert "report [reduce]" in plain
    # items live under their phase with per-item ok/fail marks
    assert "item 1/2" in plain
    assert "item 2/2" in plain
    assert "✓" in plain and "✗" in plain
    # the failing item carries its reason; the reduce summary shows too
    assert "failed: boom" in plain
    # find's header precedes report's header (grouping order preserved)
    assert plain.index("find [fanout]") < plain.index("report [reduce]")


def test_render_progress_running_phase_shows_counter():
    beats = [
        _beat("phase_start", "scan", detail="fanout", index=1, total=1),
        _beat("item_done", "scan", detail="ok", index=1, total=3),
    ]
    plain = render_progress(beats).plain
    assert "running" in plain  # no workflow_done yet
    assert "1/3" in plain  # among-N counter for the in-flight phase


def test_render_progress_marks_failed_workflow():
    beats = [
        _beat("phase_start", "verify", detail="foreach", index=1, total=1),
        _beat("phase_done", "verify", detail="error: bad ref", index=1, total=1),
        _beat("workflow_done", "verify", detail="error"),
    ]
    plain = render_progress(beats).plain
    assert "failed" in plain
    assert "verify [foreach]" in plain


def test_render_progress_empty_is_safe():
    text = render_progress([])
    assert isinstance(text, Text)
    assert "workflow" in text.plain


# --------------------------------------------------------------------------- #
# (2) Pilot: the widget mounts and updates as beats arrive
# --------------------------------------------------------------------------- #


class _Host(App):
    def compose(self) -> ComposeResult:
        yield WorkflowView(id="wf")


def test_workflowview_mounts_and_updates_on_beats():
    app = _Host()

    async def scenario():
        async with app.run_test() as pilot:
            view = app.query_one(WorkflowView)
            assert view.plain_text() == ""  # nothing rendered before any beat

            view.on_progress(_beat("phase_start", "find", detail="fanout", index=1, total=1))
            await pilot.pause()
            assert "find [fanout]" in view.plain_text()
            assert "running" in view.plain_text()

            view.on_progress(_beat("item_done", "find", detail="ok", index=1, total=1))
            view.on_progress(_beat("phase_done", "find", detail="1 result(s)", index=1, total=1))
            view.on_progress(_beat("workflow_done", "find", detail="ok"))
            await pilot.pause()
            plain = view.plain_text()
            assert "done" in plain
            assert "item 1/1" in plain

    asyncio.run(scenario())
