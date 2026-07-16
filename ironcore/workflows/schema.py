"""Workflow YAML schema + loader (IC-902) — frozen in docs/CONTRACTS.md §9.

Workflows are declarative YAML files describing deterministic orchestration over
subagents (SPEC §10). The MODEL never controls orchestration flow; the harness
walks the phases. This module owns the *schema* and *loading*; IC-903 owns
execution.

Rules for this package:
  - YAML is parsed with ``yaml.safe_load`` **exclusively** — never ``yaml.load``.
    A workflow file is untrusted input (a repo can ship ``.ironcore/workflows/``),
    so tag-driven object construction must be impossible.
  - Every load failure (YAML syntax error *or* schema violation) surfaces as a
    ``WorkflowError`` with a human, actionable message naming the file/field.
    Callers never see a raw ``yaml.YAMLError`` or pydantic ``ValidationError``.

Schema (finalized here, mirrored in CONTRACTS §9):

    name: review                       # required
    description: ...                   # optional (default "")
    inputs: [diff_ref]                 # optional list[str] (default [])
    phases:                            # required, non-empty, unique ids
      - id: find
        fanout:                        # kind 1: static fan-out over `items`
          items: [bugs, security]
          agent: {role, prompt, output_schema?}
      - id: verify
        foreach: "{{find.findings}}"   # kind 2: a {{...}} ref to a prior output
        agent: {role, prompt, output_schema?}
      - id: report
        reduce: markdown_table         # kind 3: a reducer name or inline spec

Each phase sets **exactly one** kind (`fanout` / `foreach` / `reduce`). Prompts
are template strings using ``{{var}}`` / ``{{phase.field}}`` placeholders,
substituted by :func:`interpolate` at execution time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class WorkflowError(Exception):
    """A workflow file is malformed or violates the schema.

    The message is user-facing: it names the offending file and field (or the
    unresolved variable). The `/workflow` command catches this and reports the
    message instead of a traceback.
    """


# ``{{ expr }}`` — non-greedy so ``{{a}} {{b}}`` yields two matches; surrounding
# whitespace is trimmed by the capture. Placeholders do not span newlines.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}")
_REFERENCE_RE = re.compile(r"^\{\{\s*.+?\s*\}\}$")


class AgentSpec(BaseModel):
    """One subagent: a role, a prompt template, and an optional output schema.

    ``prompt`` is a ``{{var}}`` template resolved per-item at run time.
    ``output_schema`` is an inline mapping (schema-shaped) that IC-903 validates
    subagent results against; ``None`` means results are returned unvalidated.
    """

    model_config = ConfigDict(extra="forbid")

    role: str
    prompt: str
    output_schema: dict[str, Any] | None = None


class Fanout(BaseModel):
    """Static fan-out: run ``agent`` once per element of ``items``."""

    model_config = ConfigDict(extra="forbid")

    items: list[Any]
    agent: AgentSpec


class Phase(BaseModel):
    """One orchestration phase. Exactly one of fanout/foreach/reduce is set.

    ``foreach`` is a ``{{...}}`` reference to a prior phase's output; its
    subagent is the sibling ``agent`` field. ``fanout`` nests its own agent.
    ``reduce`` names a reducer (or carries an inline spec mapping).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    fanout: Fanout | None = None
    foreach: str | None = None
    reduce: str | dict[str, Any] | None = None
    agent: AgentSpec | None = None

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> Phase:
        kinds = [k for k in ("fanout", "foreach", "reduce") if getattr(self, k) is not None]
        if not kinds:
            raise ValueError(
                f"phase {self.id!r} sets no phase-kind; set exactly one of "
                "fanout / foreach / reduce"
            )
        if len(kinds) > 1:
            raise ValueError(
                f"phase {self.id!r} sets multiple phase-kinds ({', '.join(kinds)}); "
                "exactly one is allowed"
            )
        if self.foreach is not None:
            if self.agent is None:
                raise ValueError(f"phase {self.id!r} is a foreach but has no 'agent'")
            if not _REFERENCE_RE.match(self.foreach.strip()):
                raise ValueError(
                    f"phase {self.id!r} foreach must be a {{{{phase.field}}}} reference, "
                    f"got {self.foreach!r}"
                )
        elif self.agent is not None:
            raise ValueError(
                f"phase {self.id!r} sets a top-level 'agent', which is only valid on a "
                "foreach phase (fanout nests its agent under 'fanout:')"
            )
        return self


class Workflow(BaseModel):
    """A named pipeline of phases (SPEC §10)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    inputs: list[str] = Field(default_factory=list)
    phases: list[Phase]

    @model_validator(mode="after")
    def _phases_present_and_unique(self) -> Workflow:
        if not self.phases:
            raise ValueError("workflow has no phases; at least one phase is required")
        seen: set[str] = set()
        for phase in self.phases:
            if phase.id in seen:
                raise ValueError(f"duplicate phase id {phase.id!r}")
            seen.add(phase.id)
        return self


def load_workflow(path_or_text: str | Path, *, source: str | None = None) -> Workflow:
    """Parse and validate one workflow.

    A :class:`~pathlib.Path` is read as a file (delegates to
    :func:`load_workflow_file`). A ``str`` is treated as YAML *text*; ``source``
    labels it in error messages (defaults to ``"<workflow>"``).

    Raises :class:`WorkflowError` (never a raw YAML/pydantic error) on any
    syntax or schema problem.
    """
    if isinstance(path_or_text, Path):
        return load_workflow_file(path_or_text)

    label = source or "<workflow>"
    try:
        data = yaml.safe_load(path_or_text)  # safe_load ONLY — never yaml.load
    except yaml.YAMLError as exc:
        raise WorkflowError(f"{label}: invalid YAML: {exc}") from exc

    if data is None:
        raise WorkflowError(f"{label}: workflow is empty")
    if not isinstance(data, dict):
        raise WorkflowError(
            f"{label}: workflow must be a YAML mapping, got {type(data).__name__}"
        )

    try:
        return Workflow.model_validate(data)
    except ValidationError as exc:
        raise WorkflowError(_format_validation_error(label, exc)) from None


def load_workflow_file(path: str | Path) -> Workflow:
    """Read a workflow file (UTF-8) and validate it."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(f"cannot read workflow file {path}: {exc}") from exc
    return load_workflow(text, source=str(path))


def discover_workflows(directory: str | Path) -> dict[str, Path]:
    """Map ``stem -> path`` for every ``*.yaml`` / ``*.yml`` in ``directory``.

    Used for both ``.ironcore/workflows/`` and the built-ins dir. Files are not
    parsed here (a broken file must not break discovery); the key is the file
    stem, so ``review.yaml`` is discovered as ``"review"``. On a stem clash a
    ``.yaml`` wins over a ``.yml`` (deterministic sorted order). A missing
    directory yields an empty map.
    """
    directory = Path(directory)
    found: dict[str, Path] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() in (".yaml", ".yml"):
            found.setdefault(path.stem, path)
    return found


def interpolate(template: str, context: dict[str, Any]) -> str:
    """Substitute ``{{var}}`` / ``{{phase.field}}`` placeholders from ``context``.

    Dotted names walk nested dicts (``{{find.items}}`` -> ``context["find"]
    ["items"]``). A placeholder whose name cannot be resolved raises
    :class:`WorkflowError` naming it. Resolved values are stringified.
    """

    def _replace(match: re.Match[str]) -> str:
        return str(_resolve(match.group(1).strip(), context))

    return _PLACEHOLDER_RE.sub(_replace, template)


def _resolve(expr: str, context: dict[str, Any]) -> Any:
    parts = expr.split(".")
    current: Any = context
    for index, part in enumerate(parts):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            where = ".".join(parts[: index + 1])
            raise WorkflowError(f"unknown variable {{{{{expr}}}}}: {where!r} not found in context")
    return current


def _format_validation_error(label: str, exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "(top level)"
    return f"{label}: invalid workflow at {where}: {first['msg']}"
