"""EDIT-FORMAT and CODE-SMOKE probes (IC-604, SPEC §4.1, MODELS §2).

Two mechanically-scored probes that measure the bottom of the capability stack:

``EditFormatProbe`` ("EDIT-FORMAT") fills ``edit_formats.{unified_diff,
search_replace, whole_file}``. For each format it hands the model a fixture file
plus a change request, takes the emitted edit, and scores it by the ONLY question
that matters to the engine: does the IC-302 deterministic applier
(``tools/patch.py``) apply it AND does the resulting text still parse as Python?
Score per format = fraction of trials that apply-and-parse. A ``whole_file`` reply
that leaves the file unchanged (a no-op) is a FAILURE — the model didn't make the
edit — and the same holds for a no-op diff or search/replace.

``CodeSmokeProbe`` ("CODE-SMOKE") is the floor gate, not a profile field: it asks
the model to write a small function from a docstring, then runs the returned code
and a fixed set of assertions in an isolated namespace. The pass/fail verdict is a
USABILITY FLAG carried in ``ProbeResult.notes`` (plus a synthetic ``code_smoke``
score the runner deliberately ignores — it is not a real profile field). A model
that can't clear this gate is unusable regardless of its other scores.

Design rules (shared with the rest of the suite):
  * Mechanical scoring only, no LLM judge; deterministic on scripted outputs.
  * One provider call per trial. ``MockProvider`` replays scripted completions in
    order regardless of the prompt, so trials are structured as an ordered battery.
  * Graceful on provider errors: a raised ``ProviderError`` becomes ``ok=False`` +
    a note (the runner then degrades this probe's reliability targets), never a crash.

SECURITY NOTE — CodeSmokeProbe execs model-produced code IN-PROCESS. This is a
*measurement* probe, run against a trusted local model during profiling, and is
documented as such. The exec is minimally sandboxed: a fresh globals dict, all
exceptions caught and scored as a failure, and the probe never touches the real
filesystem, network, or subprocess. It is not a general-purpose code sandbox.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ironcore.envelope.profile import EDIT_FORMAT_LADDER
from ironcore.envelope.runner import ProbeResult
from ironcore.providers.base import Message, Provider
from ironcore.providers.openai_compat import ProviderError
from ironcore.tools.patch import (
    PatchResult,
    apply_search_replace,
    apply_unified_diff,
    apply_whole_file,
)

# --------------------------------------------------------------------------- #
# EDIT-FORMAT
# --------------------------------------------------------------------------- #

_EDIT_TARGETS: tuple[str, ...] = tuple(f"edit_formats.{fmt}" for fmt in EDIT_FORMAT_LADDER)


@dataclass(frozen=True)
class EditTrial:
    """One EDIT-FORMAT trial: a fixture file plus the change the model must make.

    ``fixture`` is the current file contents, ``request`` the human-readable change
    description injected into the prompt, ``filename`` decides syntax checking — a
    ``.py`` fixture requires the applied result to ``ast.parse`` cleanly.
    """

    fixture: str
    request: str
    filename: str = "fixture.py"

    @property
    def is_python(self) -> bool:
        return self.filename.endswith(".py")


def _apply(fmt: str, original: str, edit_text: str) -> PatchResult:
    """Route an emitted edit to the matching IC-302 applier."""
    if fmt == "unified_diff":
        return apply_unified_diff(original, edit_text)
    if fmt == "search_replace":
        return apply_search_replace(original, edit_text)
    return apply_whole_file(original, edit_text)


def _trial_passes(fmt: str, trial: EditTrial, result: PatchResult) -> bool:
    """A trial passes iff the applier succeeded, the edit was a real change (not a
    no-op), and — for Python fixtures — the new text parses.

    A no-op means the model did not make the edit; the task calls this out for
    ``whole_file`` (a model can echo the file back) but it is a non-edit for every
    format, so it fails uniformly.
    """
    if not result.ok or result.new_text is None:
        return False
    if result.no_op:
        return False
    if trial.is_python:
        try:
            ast.parse(result.new_text)
        except SyntaxError:
            return False
    return True


class EditFormatProbe:
    """Score each edit format by apply-and-parse success (fills ``edit_formats``).

    Iterates the frozen ``EDIT_FORMAT_LADDER`` order (unified_diff, search_replace,
    whole_file). For each format it runs its trials in order — one provider call per
    trial — applies the emitted edit with the deterministic patcher, and records the
    fraction that apply cleanly AND still parse. A format with no trials scores 0.0.
    """

    id = "EDIT-FORMAT"
    title = "Emit edits per format; the harness patcher applies them and the result parses"
    targets = _EDIT_TARGETS

    def __init__(self, trials: Mapping[str, Sequence[EditTrial]] | None = None) -> None:
        source = _default_trials() if trials is None else trials
        # Freeze into an ordered, ladder-keyed mapping so scoring is deterministic.
        self._trials: dict[str, tuple[EditTrial, ...]] = {
            fmt: tuple(source.get(fmt, ())) for fmt in EDIT_FORMAT_LADDER
        }

    def _prompt(self, fmt: str, trial: EditTrial) -> list[Message]:
        """A well-formed prompt for real use; ignored by MockProvider's scripting."""
        system = (
            "You are an expert programmer. Edit the given file to satisfy the request. "
            f"Reply with ONLY a {fmt} edit and nothing else."
        )
        user = (
            f"File `{trial.filename}`:\n```\n{trial.fixture}\n```\n\n"
            f"Change request: {trial.request}\n\n"
            f"Emit the edit as {fmt}."
        )
        return [Message(role="system", content=system), Message(role="user", content=user)]

    async def run(self, provider: Provider) -> ProbeResult:
        scores: dict[str, float] = {}
        notes: list[str] = []
        for fmt in EDIT_FORMAT_LADDER:
            trials = self._trials[fmt]
            if not trials:
                scores[f"edit_formats.{fmt}"] = 0.0
                notes.append(f"{fmt}: no trials")
                continue
            passed = 0
            for trial in trials:
                try:
                    completion = await provider.complete(self._prompt(fmt, trial))
                except ProviderError as exc:
                    return ProbeResult(
                        self.id,
                        {},
                        notes=(
                            f"provider error during {fmt} trial: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                        ok=False,
                    )
                result = _apply(fmt, trial.fixture, completion.message.content)
                if _trial_passes(fmt, trial, result):
                    passed += 1
            scores[f"edit_formats.{fmt}"] = passed / len(trials)
            notes.append(f"{fmt}: {passed}/{len(trials)} applied+parsed")
        return ProbeResult(self.id, scores, notes="; ".join(notes), ok=True)


# --------------------------------------------------------------------------- #
# CODE-SMOKE
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SmokeTask:
    """A floor-gate coding task: a docstring to implement + assertions to satisfy.

    ``func_name`` is the function the model must define; ``checks`` are
    ``(args, expected)`` pairs — the probe calls ``func_name(*args)`` and compares to
    ``expected``. Deterministic pure-Python only (no I/O), so the verdict is stable.
    """

    docstring: str
    func_name: str
    checks: tuple[tuple[tuple[object, ...], object], ...]
    signature: str = ""


def _smoke_exec(code: str, task: SmokeTask) -> tuple[bool, str]:
    """Exec model code in an isolated namespace and run the task's assertions.

    Returns ``(passed, detail)``. Any failure mode — code that won't compile, a
    missing/uncallable function, a raised exception, or a wrong answer — is a clean
    False with an explanation, never a crash. See the module SECURITY NOTE.
    """
    namespace: dict[str, object] = {}
    try:
        compiled = compile(code, "<code-smoke>", "exec")
        exec(compiled, namespace)  # noqa: S102 — measurement probe, isolated globals
    except Exception as exc:  # noqa: BLE001 — SyntaxError et al. are a graded failure
        return False, f"code did not exec ({type(exc).__name__}: {exc})"
    fn = namespace.get(task.func_name)
    if not callable(fn):
        return False, f"function {task.func_name!r} was not defined"
    for args, expected in task.checks:
        try:
            got = fn(*args)
        except Exception as exc:  # noqa: BLE001 — a runtime error fails the gate
            return False, f"{task.func_name}{args!r} raised {type(exc).__name__}: {exc}"
        if got != expected:
            return False, f"{task.func_name}{args!r} -> {got!r}, expected {expected!r}"
    return True, f"{len(task.checks)} assertion(s) passed"


class CodeSmokeProbe:
    """The usability floor gate (fills no profile field; verdict lives in notes).

    Asks the model for one small function, then execs the returned code with a fresh
    globals dict and checks it against fixed assertions. The verdict is a pass/fail
    flag in ``notes``; ``scores`` carries a synthetic ``code_smoke`` value the runner
    ignores (``targets`` is empty, so nothing is merged into the real profile).
    """

    id = "CODE-SMOKE"
    title = "Write a small function from a docstring and pass its assertions (floor gate)"
    targets: tuple[str, ...] = ()

    def __init__(self, task: SmokeTask | None = None) -> None:
        self._task = task if task is not None else _default_smoke_task()

    def _prompt(self) -> list[Message]:
        task = self._task
        sig = f" Use the signature `{task.signature}`." if task.signature else ""
        system = "You are an expert programmer. Reply with ONLY Python code — no prose, no fences."
        user = (
            f"Write a Python function named `{task.func_name}`.{sig}\n\n"
            f"Specification:\n{task.docstring}"
        )
        return [Message(role="system", content=system), Message(role="user", content=user)]

    async def run(self, provider: Provider) -> ProbeResult:
        try:
            completion = await provider.complete(self._prompt())
        except ProviderError as exc:
            return ProbeResult(
                self.id,
                {},
                notes=f"provider error: {type(exc).__name__}: {exc}",
                ok=False,
            )
        passed, detail = _smoke_exec(completion.message.content, self._task)
        verdict = "PASS" if passed else "FAIL"
        return ProbeResult(
            self.id,
            {"code_smoke": 1.0 if passed else 0.0},  # synthetic; not a profile field
            notes=f"CODE-SMOKE {verdict}: {detail}",
            ok=True,
        )


# --------------------------------------------------------------------------- #
# Default fixtures (used by /probe, IC-608, when no explicit trials are supplied)
# --------------------------------------------------------------------------- #

_DEFAULT_FIXTURE = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'


def _default_trials() -> dict[str, list[EditTrial]]:
    """One representative trial per format, asking for the same real change so the
    scores are comparable across the ladder. Callers may pass richer batteries."""
    request = "Make `add` return the product of a and b instead of the sum."
    return {
        fmt: [EditTrial(fixture=_DEFAULT_FIXTURE, request=request)]
        for fmt in EDIT_FORMAT_LADDER
    }


def _default_smoke_task() -> SmokeTask:
    return SmokeTask(
        docstring="Return the factorial of a non-negative integer n. factorial(0) == 1.",
        func_name="factorial",
        signature="factorial(n)",
        checks=(((0,), 1), ((1,), 1), ((5,), 120)),
    )


__all__ = [
    "CodeSmokeProbe",
    "EditFormatProbe",
    "EditTrial",
    "SmokeTask",
]
