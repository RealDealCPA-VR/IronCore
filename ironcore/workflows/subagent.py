"""Subagent runner (IC-901): run ONE agent task in a fresh context.

A subagent is a single, self-contained unit of delegated work. The orchestrator
(IC-903) composes a complete instruction, hands it here with an ``engine_factory``
that mints a FRESH ``TurnEngine`` (fresh composed context + state + tools bound to
a provider/profile/workspace), and gets back a structured, validated result. Many
small well-framed contexts beat one long drifting one (SPEC §10) — so each call
here is deliberately stateless: no shared conversation, no clocks, no randomness.

What this module owns:

* ``run_subagent`` — drive the engine's bounded turn loop with the task prompt,
  collect streamed text + tool activity, cap tool actions at ``max_turns``. When a
  ``output_schema`` is declared, extract the LAST JSON object from the final text,
  validate it, and on validation failure do exactly ONE mechanical retry (re-run a
  fresh engine with the schema error appended) before failing with ``ok=False``.
* ``extract_json`` / ``validate_against`` — pure, dependency-free helpers (no
  ``jsonschema``): a balanced-brace scanner and a small required-keys + basic-type
  checker. Both are unit-tested in isolation.

Determinism: nothing here reads a clock or randomness; with ``MockProvider`` every
run is reproducible.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ironcore.core.events import TextDelta, ToolCallRequested, TurnError
from ironcore.safety.modes import Mode

if TYPE_CHECKING:  # avoid a runtime workflows->core.engine import; only used for typing
    from ironcore.core.engine import TurnEngine

#: Default tool-action budget for one subagent run (per attempt). The orchestrator
#: overrides per task; small models earn a lower cap.
DEFAULT_MAX_TURNS = 6


@dataclass
class SubagentTask:
    """One fully-composed unit of delegated work.

    ``prompt`` is the COMPLETE instruction — the orchestrator has already injected
    every ``{{item}}``/context substitution; the subagent never sees the workflow.
    ``mode`` defaults to AUTO (autonomous subagents run hands-off inside the
    workspace sandbox), but the orchestrator picks it per task; ``run_subagent``
    applies it to the freshly-built engine, so the factory need not set a mode.
    """

    role: str
    prompt: str
    output_schema: dict | None = None
    max_turns: int = DEFAULT_MAX_TURNS
    mode: Mode = Mode.AUTO


@dataclass
class SubagentResult:
    """The structured outcome of one subagent run.

    ``output`` is the parsed+validated object when a schema was given and satisfied,
    otherwise the final text. ``text`` is always the raw final assistant text.
    ``turns_used`` counts tool ACTIONS taken (summed across attempts); a subagent
    that answers directly with no tools reports 0. ``transcript_ref`` is a stable,
    deterministic label for the run (``role#attempts``).
    """

    ok: bool
    output: Any
    text: str
    turns_used: int
    error: str | None = None
    transcript_ref: str | None = None


@dataclass
class _DriveOutcome:
    """Internal: what one engine drive produced."""

    text: str
    tool_actions: int
    bounded: bool  # stopped because max_turns was reached
    error: str | None = None  # engine TurnError message, if the turn failed hard


async def run_subagent(
    task: SubagentTask,
    *,
    engine_factory: Callable[[], TurnEngine],
) -> SubagentResult:
    """Run ONE agent task with a fresh engine and return a structured result.

    ``engine_factory()`` MUST return a fresh ``TurnEngine`` on every call (the retry
    path calls it again for a clean context). The engine is driven for a single
    ``run_turn`` per attempt; ``task.max_turns`` bounds tool actions per attempt.

    Schema path: the LAST JSON object in the final text is extracted and validated.
    On failure, exactly one retry runs a fresh engine with the schema error appended
    to the prompt; a second failure yields ``ok=False`` with a clear error.
    """
    schema = task.output_schema
    prompt = task.prompt
    total_actions = 0
    attempts = 0
    last_error: str | None = None

    while attempts < 2:  # initial attempt + at most one mechanical retry
        attempts += 1
        engine = engine_factory()
        engine.mode = task.mode  # the task's declared mode wins for this run
        outcome = await _drive(engine, prompt, task.max_turns)
        total_actions += outcome.tool_actions
        ref = f"{task.role}#{attempts}"

        # A hard engine failure or a blown turn budget is not a validation problem;
        # neither is retried (the retry is specifically for schema mismatch).
        if outcome.error is not None:
            return SubagentResult(
                ok=False, output=None, text=outcome.text, turns_used=total_actions,
                error=f"engine error: {outcome.error}", transcript_ref=ref,
            )
        if outcome.bounded:
            return SubagentResult(
                ok=False, output=None, text=outcome.text, turns_used=total_actions,
                error=f"exceeded max_turns ({task.max_turns})", transcript_ref=ref,
            )
        if schema is None:
            return SubagentResult(
                ok=True, output=outcome.text, text=outcome.text,
                turns_used=total_actions, error=None, transcript_ref=ref,
            )

        obj = extract_json(outcome.text)
        if obj is None:
            err: str | None = "no JSON object found in the subagent output"
        else:
            err = validate_against(obj, schema)
        if err is None:
            return SubagentResult(
                ok=True, output=obj, text=outcome.text,
                turns_used=total_actions, error=None, transcript_ref=ref,
            )

        last_error = err
        prompt = _retry_prompt(task.prompt, schema, err)  # only used if we loop again

    return SubagentResult(
        ok=False, output=None, text=outcome.text, turns_used=total_actions,
        error=f"structured output invalid after 1 retry: {last_error}",
        transcript_ref=f"{task.role}#{attempts}",
    )


async def _drive(engine: TurnEngine, prompt: str, max_turns: int) -> _DriveOutcome:
    """Drive one ``run_turn``, collecting text and bounding tool actions.

    Tool actions are counted from ``ToolCallRequested`` events. When the count would
    exceed ``max_turns`` the drive stops BEFORE the offending call executes (the
    engine is suspended at the request yield, so the tool never runs) and the
    generator is closed cleanly.
    """
    text_parts: list[str] = []
    actions = 0
    bounded = False
    error: str | None = None
    agen = engine.run_turn(prompt)
    try:
        async for ev in agen:
            if isinstance(ev, TextDelta):
                text_parts.append(ev.text)
            elif isinstance(ev, ToolCallRequested):
                if actions >= max_turns:
                    bounded = True
                    break
                actions += 1
            elif isinstance(ev, TurnError):
                error = ev.message
    finally:
        await agen.aclose()  # if we broke early, unwind the suspended engine cleanly
    return _DriveOutcome(
        text="".join(text_parts), tool_actions=actions, bounded=bounded, error=error
    )


def _retry_prompt(original: str, schema: dict, error: str) -> str:
    """Re-frame the task with the validation error so the retry can self-correct."""
    return (
        f"{original}\n\n"
        f"[schema-retry] Your previous reply was rejected: {error}\n"
        "Reply again with ONLY a single JSON object that satisfies this schema:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )


# --------------------------------------------------------------------------- #
# extract_json: the last balanced {...} in the text that parses to an object
# --------------------------------------------------------------------------- #


def extract_json(text: str) -> dict | None:
    """Return the LAST top-level ``{...}`` span in ``text`` that parses to a dict.

    Scans balanced braces while ignoring braces inside JSON strings, so prose and
    string-embedded ``{`` never confuse it. Non-parsing candidates are skipped, so a
    later malformed brace group does not shadow an earlier valid object.
    """
    candidates: list[str] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    candidates.append(text[start : i + 1])
                    start = -1
    result: dict | None = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            result = obj  # keep the LAST one that parses
    return result


# --------------------------------------------------------------------------- #
# validate_against: a tiny required-keys + basic-type checker (no jsonschema)
# --------------------------------------------------------------------------- #

#: JSON-schema primitive type -> Python predicate. ``bool`` is deliberately NOT an
#: ``integer``/``number`` (JSON distinguishes them; Python does not).
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _typename(value: Any) -> str:
    for name, check in _TYPE_CHECKS.items():
        if check(value):
            return name
    return type(value).__name__


def _fmt_type(expected: Any) -> str:
    if isinstance(expected, list):
        return "/".join(str(e) for e in expected)
    return str(expected)


def _type_ok(value: Any, expected: Any) -> bool:
    """True if ``value`` matches ``expected`` (a type name or a list of them).

    An unknown/absent type is permissive — we only enforce what we understand.
    """
    if isinstance(expected, list):
        return any(_type_ok(value, e) for e in expected)
    check = _TYPE_CHECKS.get(expected)
    return check is None or check(value)


def validate_against(obj: Any, schema: dict | None) -> str | None:
    """Validate ``obj`` against a small subset of JSON Schema.

    Understands ``type`` (primitive name or list), ``required`` (keys that must be
    present), ``properties`` (per-key ``type`` + nested object/array checks) and
    array ``items``. Returns a human-readable error string, or ``None`` when valid.
    An unrecognized/empty schema imposes no constraint.
    """
    if not isinstance(schema, dict):
        return None
    expected = schema.get("type")
    if expected is not None and not _type_ok(obj, expected):
        return f"expected type {_fmt_type(expected)!r}, got {_typename(obj)}"
    if isinstance(obj, dict):
        return _validate_object(obj, schema)
    return None


def _validate_object(obj: dict, schema: dict) -> str | None:
    for key in schema.get("required", []) or []:
        if key not in obj:
            return f"missing required key {key!r}"
    props = schema.get("properties")
    if isinstance(props, dict):
        for key, subschema in props.items():
            if key not in obj or not isinstance(subschema, dict):
                continue
            err = _validate_value(obj[key], subschema, str(key))
            if err is not None:
                return err
    return None


def _validate_value(value: Any, subschema: dict, path: str) -> str | None:
    expected = subschema.get("type")
    if expected is not None and not _type_ok(value, expected):
        return f"key {path!r} expected type {_fmt_type(expected)!r}, got {_typename(value)}"
    if isinstance(value, dict):
        nested = _validate_object(value, subschema)
        if nested is not None:
            return f"{path}: {nested}"
    elif isinstance(value, list):
        items = subschema.get("items")
        if isinstance(items, dict):
            for index, element in enumerate(value):
                err = _validate_value(element, items, f"{path}[{index}]")
                if err is not None:
                    return err
    return None
