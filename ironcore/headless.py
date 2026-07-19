"""Headless turn runner behind ``ironcore exec`` (SPEC §5; CONTRACTS §4).

``core/events.py`` already names "headless mode" as a consumer of the engine's
event stream, and ``ironcore/demo/scenario.py`` is a working headless renderer;
this module is the production one behind the ``exec`` subcommand. It
async-iterates ``TurnEngine.run_turn`` and renders the stream two ways:

- **default (human):** streamed ``TextDelta`` text goes to **stdout** — that is
  the model's answer, the thing a shell pipeline consumes; every other event
  (tool calls, approvals, verify/repair status, the completion line) goes to
  **stderr**, so ``ironcore exec "…" > answer.txt`` captures only the answer.
- **``--json``:** one serialized event per line to **stdout** (the event
  dataclasses are an additive contract), for a machine consumer; nothing else
  is written to stdout.

Approvals are fail-closed by construction and invent no new decision path: the
engine's own ``ApprovalBroker`` is built with ``timeout=0`` so any ``ask`` gate
(there is no human to prompt) resolves through the broker's existing
timeout-DENY path. Default ``--mode`` is PLAN (read-only, CI-safe); ``--mode``
raises it. When an ``ApprovalRequired`` is seen, a one-line hint is printed to
stderr so the denial is never silent.

Exit codes (owned by ``run_exec``): 0 on ``TurnCompleted``, 1 on ``TurnError``.
``cli.cmd_exec`` maps a ``ConfigError`` during setup to 2 before we are called.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from ironcore.core.events import (
    ApprovalRequired,
    Event,
    ResampleProgress,
    TextDelta,
    ToolCallFinished,
    ToolCallRequested,
    TurnCompleted,
    TurnError,
    TurnStarted,
)

if TYPE_CHECKING:
    from ironcore.config.settings import Settings
    from ironcore.core.engine import TurnEngine
    from ironcore.providers.registry import ProviderRegistry
    from ironcore.safety.modes import Mode


def _call_dict(call: object) -> dict[str, object]:
    """A ToolCall as a JSON-safe dict (arguments are model JSON, already safe)."""
    return {
        "id": getattr(call, "id", ""),
        "name": getattr(call, "name", ""),
        "arguments": getattr(call, "arguments", {}),
    }


def serialize_event(event: Event) -> dict[str, object]:
    """One event -> a JSON-safe dict with a ``type`` discriminator.

    Only the frozen event fields are emitted; ``ToolResult`` is reduced to its
    model-visible text semantics (``ok``/``output``/``error``) so a large or
    binary ``data`` payload never bloats the stream.
    """
    if isinstance(event, TurnStarted):
        return {"type": "turn_started", "turn_id": event.turn_id, "mode": event.mode}
    if isinstance(event, TextDelta):
        return {"type": "text_delta", "turn_id": event.turn_id, "text": event.text}
    if isinstance(event, ToolCallRequested):
        return {
            "type": "tool_call_requested",
            "turn_id": event.turn_id,
            "call": _call_dict(event.call),
            "risk": event.risk,
            "decision": event.decision,
        }
    if isinstance(event, ApprovalRequired):
        return {
            "type": "approval_required",
            "turn_id": event.turn_id,
            "call": _call_dict(event.call),
            "risk": event.risk,
            "preview": event.preview,
        }
    if isinstance(event, ToolCallFinished):
        return {
            "type": "tool_call_finished",
            "turn_id": event.turn_id,
            "call": _call_dict(event.call),
            "result": {
                "ok": event.result.ok,
                "output": event.result.output,
                "error": event.result.error,
            },
        }
    if isinstance(event, TurnCompleted):
        return {
            "type": "turn_completed",
            "turn_id": event.turn_id,
            "usage": dict(event.usage),
            "stop_reason": event.stop_reason,
        }
    if isinstance(event, TurnError):
        return {
            "type": "turn_error",
            "turn_id": event.turn_id,
            "message": event.message,
            "data": event.data,
        }
    if isinstance(event, ResampleProgress):
        return {
            "type": "resample_progress",
            "turn_id": event.turn_id,
            "seam": event.seam,
            "attempt": event.attempt,
            "total": event.total,
        }
    # Additive-contract safety net: an unknown future event still serializes.
    return {"type": type(event).__name__, "repr": repr(event)}  # pragma: no cover


def _render_human(event: Event, out: TextIO, err: TextIO) -> None:
    """Stream the model's prose to ``out``; everything else, as status, to ``err``."""
    if isinstance(event, TextDelta):
        out.write(event.text)
        out.flush()
        return
    if isinstance(event, TurnStarted):
        err.write(f"[turn {event.turn_id}] mode={event.mode}\n")
    elif isinstance(event, ToolCallRequested):
        err.write(f"[tool] {event.call.name} risk={event.risk} -> {event.decision}\n")
    elif isinstance(event, ApprovalRequired):
        # No human to prompt in headless: the broker will DENY (fail closed).
        # Say so, and how to change it, so the denial is never a silent no-op.
        err.write(
            f"[approval] {event.call.name} ({event.risk}) needs approval; "
            "headless auto-denies. Raise --mode or run `ironcore` interactively "
            f"to allow it. {event.preview}\n"
        )
    elif isinstance(event, ToolCallFinished):
        status = "ok" if event.result.ok else "failed"
        detail = (event.result.output or event.result.error or "").strip().splitlines()
        head = detail[0] if detail else ""
        err.write(f"[tool] {event.call.name} {status}: {head}\n")
    elif isinstance(event, ResampleProgress):
        err.write(f"[resample] {event.seam} candidate {event.attempt}/{event.total}\n")
    elif isinstance(event, TurnCompleted):
        err.write(f"[done] stop_reason={event.stop_reason} usage={dict(event.usage)}\n")
    elif isinstance(event, TurnError):
        err.write(f"[error] {event.message}\n")
    err.flush()


async def _drive(
    engine: TurnEngine,
    prompt: str,
    *,
    json_output: bool,
    out: TextIO,
    err: TextIO,
    registry: ProviderRegistry | None,
) -> int:
    """Run one turn, render each event, and return the exit code.

    ``TurnEngine.run_turn`` always terminates with exactly one ``TurnCompleted``
    (exit 0) or one ``TurnError`` (exit 1); the default 1 is a defensive floor
    for a stream that somehow ends with neither.
    """
    code = 1
    try:
        async for event in engine.run_turn(prompt):
            if json_output:
                out.write(json.dumps(serialize_event(event), default=str) + "\n")
                out.flush()
            else:
                _render_human(event, out, err)
            if isinstance(event, TurnCompleted):
                code = 0
            elif isinstance(event, TurnError):
                code = 1
        # a human run that streamed prose ends without a trailing newline
        if not json_output:
            out.write("\n")
            out.flush()
    finally:
        if registry is not None:
            await registry.close_all()
    return code


def run_exec(
    engine: TurnEngine,
    prompt: str,
    *,
    json_output: bool = False,
    out: TextIO | None = None,
    err: TextIO | None = None,
    registry: ProviderRegistry | None = None,
) -> int:
    """Drive ``engine`` through one headless turn; return the exit code.

    ``out``/``err`` default to the real stdout/stderr; tests pass ``StringIO``.
    ``registry`` (when given) is closed inside the same event loop so its httpx
    clients never leak — a test that injects a ``MockProvider`` engine passes
    ``None``.
    """
    return asyncio.run(
        _drive(
            engine,
            prompt,
            json_output=json_output,
            out=out if out is not None else sys.stdout,
            err=err if err is not None else sys.stderr,
            registry=registry,
        )
    )


def build_engine(
    settings: Settings, workspace: Path, mode: Mode
) -> tuple[TurnEngine, ProviderRegistry]:
    """Build a real, minimal headless engine from ``Settings`` (production path).

    Import-light: the engine and its dependencies are imported here, inside the
    handler, so ``ironcore --version`` / ``doctor`` never pay for them. Uses the
    model's cached envelope if one exists (else floor-conservative defaults), a
    ``timeout=0`` approval broker (asks fail closed — see the module docstring),
    no shadow-git snapshots, and no ``HANDOFF.md`` write: a one-shot exec should
    not mutate session artifacts. The caller closes the returned registry.
    """
    from ironcore.core.approvals import ApprovalBroker
    from ironcore.core.engine import TurnEngine
    from ironcore.envelope.profile import CapabilityProfile
    from ironcore.envelope.suite import default_envelope_dir
    from ironcore.providers.registry import ProviderRegistry
    from ironcore.tools.default import build_default_registry

    registry = ProviderRegistry.from_settings(settings)
    tools = build_default_registry(settings, workspace)
    model = settings.provider.model
    profile = CapabilityProfile.load(default_envelope_dir(), model) or CapabilityProfile(
        model_id=model
    )
    engine = TurnEngine(
        registry.default,
        tools,
        settings,
        profile,
        mode,
        workspace=workspace,
        approvals=ApprovalBroker(timeout=0.0),
        snapshots=None,
        handoff_path=None,
    )
    return engine, registry
