"""Workflow progress view (IC-904): a grouped tree of orchestration beats.

A workflow run emits :class:`~ironcore.workflows.engine.WorkflowProgress` beats
(``phase_start`` / ``item_done`` / ``phase_done`` / ``workflow_done``). This widget
folds a running list of those beats into a compact, grouped tree — one header per
phase, its fan-out/foreach items marked ✓/✗ beneath it, and an among-N counter
while a phase is still in flight (SPEC §10: "grouped progress").

Two surfaces, one pure core:

* ``render_progress(beats)`` — the pure, testable transform: an ordered list of
  beats → a ``rich.text.Text`` grouped tree. No widget, no app, no clock.
* ``WorkflowView`` — a ``Static`` that accumulates beats via ``on_progress`` and
  re-renders through ``render_progress``. ``plain_text`` mirrors the current text
  so front ends and tests read it as a string.

Rendering only — this widget holds no engine reference and mounts nothing from
``core/`` (docs/ARCHITECTURE.md §4). Model/tool text is only ever wrapped in
``Text`` (never interpreted as Rich console markup).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.widgets import Static

from ironcore.workflows.engine import WorkflowProgress

#: Marks + their styles for an item / phase outcome.
_OK = "green"
_FAIL = "red"
_RUNNING = "yellow"
_HEADER = "bold"
_DIM = "dim"


@dataclass
class _PhaseState:
    """Accumulated beats for one phase, in first-seen order."""

    phase_id: str
    kind: str = ""
    #: item index (1-based) -> (ok, detail), collected as ``item_done`` beats land.
    items: dict[int, tuple[bool, str]] = field(default_factory=dict)
    item_total: int | None = None
    done: bool = False
    ok: bool = True
    summary: str = ""


def _fold(beats: list[WorkflowProgress]) -> tuple[list[_PhaseState], str | None]:
    """Fold a flat beat list into per-phase state + the terminal workflow status.

    The returned status is ``None`` while running, ``"ok"``/``"error"`` once the
    ``workflow_done`` beat has landed.
    """
    order: list[str] = []
    phases: dict[str, _PhaseState] = {}
    status: str | None = None

    def phase(pid: str) -> _PhaseState:
        if pid not in phases:
            phases[pid] = _PhaseState(phase_id=pid)
            order.append(pid)
        return phases[pid]

    for beat in beats:
        if beat.kind == "phase_start":
            state = phase(beat.phase_id)
            state.kind = beat.detail
        elif beat.kind == "item_done":
            state = phase(beat.phase_id)
            ok = not beat.detail.startswith("failed")
            if beat.index is not None:
                state.items[beat.index] = (ok, beat.detail)
            state.item_total = beat.total
        elif beat.kind == "phase_done":
            state = phase(beat.phase_id)
            state.done = True
            state.ok = not beat.detail.startswith("error")
            state.summary = beat.detail
        elif beat.kind == "workflow_done":
            status = "error" if beat.detail == "error" else "ok"

    return [phases[pid] for pid in order], status


def render_progress(beats: list[WorkflowProgress]) -> Text:
    """Render a list of progress beats into a grouped tree as ``Text``.

    Pure and unit-testable: no widget or app required. Phase headers carry a
    ✓ done / ✗ failed / ⋯ running mark and, while running, an ``N/total`` item
    counter; item lines carry per-item ✓/✗ marks and any failure detail.
    """
    phases, status = _fold(beats)
    text = Text()

    if status is None:
        text.append("workflow ⋯ running\n", style=_HEADER)
    elif status == "ok":
        text.append("workflow ✓ done\n", style=f"{_HEADER} {_OK}")
    else:
        text.append("workflow ✗ failed\n", style=f"{_HEADER} {_FAIL}")

    for state in phases:
        _append_phase(text, state)
    return text


def _append_phase(text: Text, state: _PhaseState) -> None:
    mark, style = _phase_mark(state)
    kind = f" [{state.kind}]" if state.kind else ""
    counter = _phase_counter(state)
    text.append(f"{mark} {state.phase_id}{kind}", style=style)
    if counter:
        text.append(f"  {counter}", style=_DIM)
    if state.done and state.summary:
        text.append(f"  — {state.summary}", style=_DIM)
    text.append("\n")

    for index in sorted(state.items):
        ok, detail = state.items[index]
        item_mark = "✓" if ok else "✗"
        item_style = _OK if ok else _FAIL
        total = f"/{state.item_total}" if state.item_total else ""
        text.append(f"    {item_mark} item {index}{total}", style=item_style)
        if not ok and detail:
            text.append(f" — {detail}", style=_DIM)
        text.append("\n")


def _phase_mark(state: _PhaseState) -> tuple[str, str]:
    if not state.done:
        return "⋯", _RUNNING
    if state.ok:
        return "✓", _OK
    return "✗", _FAIL


def _phase_counter(state: _PhaseState) -> str:
    """An ``N/total`` item counter shown while a phase is still running."""
    if state.done or state.item_total is None:
        return ""
    return f"{len(state.items)}/{state.item_total}"


class WorkflowView(Static):
    """A live grouped view of a workflow's progress beats.

    ``on_progress(beat)`` appends the beat and re-renders; the whole tree is small,
    so a full re-render per beat is cheap and keeps the render pure. ``plain_text``
    is the read surface for tests.
    """

    DEFAULT_CSS = """
    WorkflowView {
        height: auto;
        padding: 0 1;
        color: $text;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._beats: list[WorkflowProgress] = []
        self._plain = ""

    def on_progress(self, beat: WorkflowProgress) -> None:
        """Record one beat and refresh the rendered tree."""
        self._beats.append(beat)
        rendered = render_progress(self._beats)
        self._plain = rendered.plain
        self.update(rendered)

    def render_text(self) -> Text:
        """The current grouped tree as ``Text`` (read surface for tests)."""
        return render_progress(self._beats)

    def plain_text(self) -> str:
        """The current grouped tree as a plain string (read surface for tests)."""
        return self._plain
