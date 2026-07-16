"""Workflow orchestrator (IC-903): execute a validated ``Workflow`` over subagents.

Workflows are YAML files in ``.ironcore/workflows/`` describing deterministic
orchestration over subagents (SPEC §10). The MODEL never controls the flow — the
harness does, and this matters twice as much for small models, whose long-horizon
planning is exactly the thing we do not trust.

:class:`WorkflowRunner` walks a schema-validated :class:`~ironcore.workflows.schema.Workflow`:

* Phases run **sequentially**, accumulating a ``context`` dict (the workflow
  ``inputs`` plus each completed phase's output, keyed by ``phase.id``).
* Within a phase, subagent items run **concurrently**, capped by an
  ``asyncio.Semaphore``; results are collected in item order regardless of which
  finishes first (``asyncio.gather`` preserves argument order).
* Every subagent runs with a FRESH ``TurnEngine`` minted by ``engine_factory``
  (passed straight to :func:`~ironcore.workflows.subagent.run_subagent`) — many
  small well-framed contexts beat one long drifting one.

Failure model (two tiers):

* **Per-item** (a subagent raises, or returns ``ok=False``, or its prompt has an
  unresolved ``{{var}}``) → that slot becomes ``None`` in the results list, a note
  is recorded, and the phase/workflow carries on. One bad item never aborts a run.
* **Per-phase / structural** (a ``foreach`` ref missing from the context or not a
  list; a ``reduce`` with no prior phase; an unknown reducer) is a workflow-author
  error: the reason is recorded as a note, remaining phases are skipped, and the
  result is ``ok=False``. ``run`` **returns** this partial result rather than
  raising, so a progress-driven caller (IC-904) always gets ``outputs`` + the
  reason and never has to guard ``run`` with try/except mid-stream.

Progress is a plain callback (:class:`WorkflowProgress`), deliberately decoupled
from ``core.events`` — a workflow is not a turn.

Determinism: nothing here reads a clock or randomness; with ``MockProvider``-backed
engines every run — including the concurrent phases — is reproducible.

Rules for this package: never construct objects from workflow YAML tags; consume
``core`` read-only (the ``TurnEngine`` import is typing-only, keeping the
workflows→core boundary a compile-time edge, per ``docs/ARCHITECTURE.md`` §4).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ironcore.safety.modes import Mode
from ironcore.workflows.schema import AgentSpec, Phase, Workflow, WorkflowError, interpolate
from ironcore.workflows.subagent import SubagentTask, run_subagent

if TYPE_CHECKING:  # typing-only: keep workflows from importing core at runtime
    from ironcore.core.engine import TurnEngine

#: A ``{{ expr }}`` reference occupying the WHOLE string (a ``foreach`` value).
_REFERENCE_RE = re.compile(r"^\{\{\s*(.+?)\s*\}\}$")

#: Progress ``kind`` values, in the order they fire across a run.
PROGRESS_KINDS = ("phase_start", "item_done", "phase_done", "workflow_done")


@dataclass
class WorkflowProgress:
    """One orchestration progress beat, handed to the ``on_progress`` callback.

    ``kind`` is one of :data:`PROGRESS_KINDS`. ``phase_start``/``phase_done`` carry
    ``index``/``total`` as the phase's 1-based position within the workflow;
    ``item_done`` carries the item's 1-based index and the phase's item ``total``.
    ``detail`` is a short human string (the phase kind, ``"ok"``, or a failure
    reason). ``item_done`` fires only for subagent items (fanout/foreach), never
    for a reduce.
    """

    phase_id: str
    kind: str
    detail: str = ""
    index: int | None = None
    total: int | None = None


@dataclass
class WorkflowResult:
    """The outcome of one workflow run.

    ``ok`` is ``True`` unless a structural/terminal error aborted the run (per-item
    subagent failures are isolated and do NOT flip it). ``outputs`` maps
    ``phase.id -> value`` for every phase that completed. ``notes`` collects every
    per-item failure and any terminal reason, in occurrence order. ``final`` is the
    last completed phase's output (``None`` if none completed).
    """

    ok: bool
    outputs: dict[str, Any]
    notes: list[str]
    final: Any

    def render_summary(self) -> str:
        """A compact multi-line digest of the run for a log or a `/workflow` reply."""
        lines = [f"workflow {'ok' if self.ok else 'FAILED'}"]
        for phase_id, value in self.outputs.items():
            lines.append(f"  {phase_id}: {_shorten(value)}")
        if self.notes:
            lines.append(f"notes ({len(self.notes)}):")
            lines.extend(f"  - {note}" for note in self.notes)
        return "\n".join(lines)


class WorkflowRunner:
    """Execute a validated :class:`Workflow` deterministically over subagents.

    ``engine_factory`` MUST return a FRESH ``TurnEngine`` on every call (subagents,
    and the subagent retry path, each need a clean context); it is handed straight
    to :func:`run_subagent`. ``concurrency`` caps in-flight subagents per phase
    (floored at 1). ``on_progress`` — if given — receives a :class:`WorkflowProgress`
    at each beat; it must be a plain synchronous callback (workflows are not turns,
    so this never touches ``core.events``).
    """

    def __init__(
        self,
        *,
        engine_factory: Callable[[], TurnEngine],
        concurrency: int = 4,
        on_progress: Callable[[WorkflowProgress], None] | None = None,
        mode: Mode = Mode.AUTO,
    ) -> None:
        self._engine_factory = engine_factory
        self._concurrency = max(1, concurrency)
        self._on_progress = on_progress
        # Subagents run in the SESSION's mode (SAFETY T8): a PLAN session cannot
        # mutate through a workflow, because run_subagent stamps this onto the
        # fresh engine and the gate denies WRITE/EXEC/NET in PLAN. The /workflow
        # command sources this from the live engine's mode.
        self._mode = mode

    async def run(self, workflow: Workflow, inputs: dict[str, Any]) -> WorkflowResult:
        """Run every phase in order and return the accumulated result.

        ``inputs`` seeds the context; each phase's output is added under ``phase.id``
        for later phases (and reducers) to reference. Never raises for a per-item
        failure; a structural error stops the run with ``ok=False`` (see the module
        docstring).
        """
        context: dict[str, Any] = dict(inputs)
        outputs: dict[str, Any] = {}
        notes: list[str] = []
        final: Any = None
        ok = True
        total_phases = len(workflow.phases)

        for position, phase in enumerate(workflow.phases, start=1):
            kind = _phase_kind(phase)
            self._emit(phase.id, "phase_start", detail=kind, index=position, total=total_phases)
            try:
                if phase.fanout is not None:
                    value = await self._run_fanout(phase, context, notes)
                elif phase.foreach is not None:
                    value = await self._run_foreach(phase, context, notes)
                else:
                    value = self._run_reduce(phase, workflow.phases, position, outputs)
            except WorkflowError as exc:
                notes.append(f"phase {phase.id!r}: {exc}")
                ok = False
                self._emit(
                    phase.id, "phase_done", detail=f"error: {exc}",
                    index=position, total=total_phases,
                )
                break

            context[phase.id] = value
            outputs[phase.id] = value
            final = value
            self._emit(
                phase.id, "phase_done", detail=_phase_summary(value),
                index=position, total=total_phases,
            )

        self._emit(workflow.phases[-1].id, "workflow_done", detail="ok" if ok else "error")
        return WorkflowResult(ok=ok, outputs=outputs, notes=notes, final=final)

    # -- phase kinds ----------------------------------------------------------

    async def _run_fanout(
        self, phase: Phase, context: dict[str, Any], notes: list[str]
    ) -> list[Any]:
        """Run ``phase.fanout.agent`` once per static item; collect outputs in order."""
        fanout = phase.fanout
        assert fanout is not None  # guarded by the caller
        item_contexts = [{"item": item, **context} for item in fanout.items]
        return await self._run_agents(phase.id, fanout.agent, item_contexts, notes)

    async def _run_foreach(
        self, phase: Phase, context: dict[str, Any], notes: list[str]
    ) -> list[Any]:
        """Resolve the ``{{...}}`` ref to a list, then run ``phase.agent`` per element."""
        assert phase.foreach is not None and phase.agent is not None  # schema-guaranteed
        elements = _resolve_reference(phase.foreach, context)
        if not isinstance(elements, list):
            raise WorkflowError(
                f"foreach {phase.foreach!r} did not resolve to a list "
                f"(got {type(elements).__name__}); a foreach must iterate a list output"
            )
        item_contexts = [{"item": element, **context} for element in elements]
        return await self._run_agents(phase.id, phase.agent, item_contexts, notes)

    def _run_reduce(
        self, phase: Phase, phases: list[Phase], position: int, outputs: dict[str, Any]
    ) -> Any:
        """Combine the PRIOR phase's outputs with the phase's reducer."""
        if position < 2:
            raise WorkflowError("a reduce phase has no prior phase to reduce")
        prior = phases[position - 2]
        if prior.id not in outputs:
            raise WorkflowError(f"reduce cannot find prior phase {prior.id!r} output")
        prior_value = outputs[prior.id]
        values = prior_value if isinstance(prior_value, list) else [prior_value]
        return apply_reduce(phase.reduce, values)

    # -- the concurrent item loop ---------------------------------------------

    async def _run_agents(
        self,
        phase_id: str,
        agent: AgentSpec,
        item_contexts: list[dict[str, Any]],
        notes: list[str],
    ) -> list[Any]:
        """Run one subagent per item context, capped at ``self._concurrency``.

        Results land in ``results[idx]`` (item order, gather-order-independent).
        PER-ITEM FAILURE ISOLATION: any raised exception, any ``ok=False`` result,
        or an unresolved prompt variable becomes a ``None`` slot plus a note — the
        phase always runs every item and never aborts on one bad one.
        """
        total = len(item_contexts)
        results: list[Any] = [None] * total
        semaphore = asyncio.Semaphore(self._concurrency)

        async def worker(index: int, item_context: dict[str, Any]) -> None:
            async with semaphore:
                detail = await self._run_one(phase_id, index, agent, item_context, results, notes)
            self._emit(phase_id, "item_done", detail=detail, index=index + 1, total=total)

        await asyncio.gather(*(worker(i, ctx) for i, ctx in enumerate(item_contexts)))
        return results

    async def _run_one(
        self,
        phase_id: str,
        index: int,
        agent: AgentSpec,
        item_context: dict[str, Any],
        results: list[Any],
        notes: list[str],
    ) -> str:
        """Run a single subagent item; record its output or a failure note.

        Returns the ``item_done`` detail string. The broad ``except`` is deliberate:
        ``run_subagent`` does NOT catch raw engine/factory exceptions, so isolation
        lives here (``Exception`` excludes ``CancelledError``, so IC-904 cancel still
        propagates).
        """
        try:
            prompt = interpolate(agent.prompt, item_context)
            task = SubagentTask(
                role=agent.role,
                prompt=prompt,
                output_schema=agent.output_schema,
                mode=self._mode,  # SAFETY T8: subagent inherits the session mode
            )
            result = await run_subagent(task, engine_factory=self._engine_factory)
        except Exception as exc:  # noqa: BLE001 — per-item isolation is the whole point
            results[index] = None
            notes.append(f"{phase_id}[{index}] failed: {exc}")
            return f"failed: {exc}"
        if result.ok:
            results[index] = result.output
            return "ok"
        results[index] = None
        notes.append(f"{phase_id}[{index}] failed: {result.error}")
        return f"failed: {result.error}"

    # -- progress -------------------------------------------------------------

    def _emit(
        self,
        phase_id: str,
        kind: str,
        *,
        detail: str = "",
        index: int | None = None,
        total: int | None = None,
    ) -> None:
        if self._on_progress is not None:
            self._on_progress(
                WorkflowProgress(
                    phase_id=phase_id, kind=kind, detail=detail, index=index, total=total
                )
            )


# --------------------------------------------------------------------------- #
# reducers: combine a prior phase's list of outputs into one value
# --------------------------------------------------------------------------- #

#: Reducer names understood by :func:`apply_reduce` (a string reducer or the ``op``
#: of an inline dict spec). ``concat`` and ``list`` are aliases (flatten one level).
REDUCERS = ("concat", "list", "count", "markdown_table")


def apply_reduce(spec: str | dict[str, Any] | None, values: list[Any]) -> Any:
    """Reduce ``values`` (a prior phase's outputs) per ``spec``.

    ``spec`` is either a reducer NAME (``"concat"`` / ``"list"`` / ``"count"`` /
    ``"markdown_table"``) or an inline dict carrying an ``op`` key plus optional
    params (``markdown_table`` honors ``columns: [..]``). Failed (``None``) slots
    are dropped before reducing. An unknown/malformed spec raises
    :class:`WorkflowError` (surfaced by :meth:`WorkflowRunner.run` as a note).
    """
    name, params = _reducer_name(spec)
    present = [value for value in values if value is not None]
    if name in ("concat", "list"):
        flat: list[Any] = []
        for value in present:
            if isinstance(value, list):
                flat.extend(value)
            else:
                flat.append(value)
        return flat
    if name == "count":
        return len(present)
    if name == "markdown_table":
        columns = params.get("columns") if isinstance(params, dict) else None
        return _markdown_table(present, columns)
    raise WorkflowError(f"unknown reducer {name!r}; known reducers: {', '.join(REDUCERS)}")


def _reducer_name(spec: str | dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Normalize a reduce spec to ``(name, params)``."""
    if isinstance(spec, str):
        return spec.strip().lower(), {}
    if isinstance(spec, dict):
        name = spec.get("op") or spec.get("kind") or spec.get("type")
        if not isinstance(name, str):
            raise WorkflowError(
                f"reduce spec must name a reducer via 'op'; got {spec!r}"
            )
        return name.strip().lower(), spec
    raise WorkflowError(f"reduce spec must be a string or mapping, got {type(spec).__name__}")


def _markdown_table(rows: list[Any], columns: list[str] | None) -> str:
    """Render ``rows`` (dicts, or scalars under a ``value`` column) as a GFM table."""
    dict_rows = [row if isinstance(row, dict) else {"value": row} for row in rows]
    if columns:
        cols = [str(column) for column in columns]
    else:
        cols = []
        for row in dict_rows:
            for key in row:
                if key not in cols:
                    cols.append(str(key))
    if not cols:
        cols = ["value"]

    def cell(value: Any) -> str:
        return str(value).replace("\n", " ").replace("|", "\\|")

    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    lines.extend(
        "| " + " | ".join(cell(row.get(col, "")) for col in cols) + " |" for row in dict_rows
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _phase_kind(phase: Phase) -> str:
    if phase.fanout is not None:
        return "fanout"
    if phase.foreach is not None:
        return "foreach"
    return "reduce"


def _resolve_reference(reference: str, context: dict[str, Any]) -> Any:
    """Resolve a whole-string ``{{a.b.c}}`` reference to its RAW value in ``context``.

    Unlike :func:`~ironcore.workflows.schema.interpolate` (string-only), this returns
    the live object (a list, dict, ...) so a ``foreach`` can iterate it. Dotted names
    walk nested dicts; an unresolved name raises :class:`WorkflowError` naming it.
    """
    match = _REFERENCE_RE.match(reference.strip())
    if match is None:
        raise WorkflowError(f"foreach must be a {{{{ref}}}} reference, got {reference!r}")
    expr = match.group(1).strip()
    current: Any = context
    parts = expr.split(".")
    for depth, part in enumerate(parts):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            where = ".".join(parts[: depth + 1])
            raise WorkflowError(
                f"unknown variable {{{{{expr}}}}}: {where!r} not found in context"
            )
    return current


def _phase_summary(value: Any) -> str:
    if isinstance(value, list):
        failed = sum(1 for element in value if element is None)
        tail = f", {failed} failed" if failed else ""
        return f"{len(value)} result(s){tail}"
    return _shorten(value)


def _shorten(value: Any, limit: int = 60) -> str:
    if isinstance(value, list):
        return f"[{len(value)} item(s)]"
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
