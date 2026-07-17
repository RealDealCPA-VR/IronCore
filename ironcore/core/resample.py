"""Best-of-N escape hatches (MS-4): candidate generation + mechanical verifiers.

When a weak model dead-ends at a seam that has a MECHANICAL verifier — a tool
call that will not parse, a patch that will not apply — the engine resamples
fresh candidates at raised temperature and races them: the first candidate the
verifier passes wins; losers are discarded. This module is the pure/provider-
only half of that loop. The ENGINE owns everything stateful: the per-turn
candidate budget, the safety gate (a winner still goes through ``decide``), the
conversation history, and the events.

RULES
-----
- One candidate = one non-streaming ``provider.complete`` call, parsed with the
  SAME per-rung shapes the engine uses (native ``tool_calls`` /
  ``ironcall.parse`` / ``guided.parse_guided_tool_call``). A candidate that
  does not yield at least one clean call FAILS — as data, never an exception
  (``ProviderError`` becomes ``Candidate.error``).
- Verifiers are pure: ``verify_edit_candidate`` pre-applies the patch against
  the CURRENT text in memory via the deterministic ``tools.patch`` appliers —
  NOTHING is written here. Only the built-in edit formats are verifiable
  (plugin formats — MS-5 — cannot be pre-applied and therefore never pass).
- No engine import (the engine imports THIS module, never the reverse) and no
  tui import. Imports: providers (wire types), core.ironcall/guided (parsers),
  tools.patch (pure appliers).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ironcore.core import guided, ironcall
from ironcore.providers.base import Message, Provider, SamplingPolicy, ToolCall
from ironcore.providers.openai_compat import ProviderError
from ironcore.tools.patch import apply_search_replace, apply_unified_diff, apply_whole_file

__all__ = [
    "Candidate",
    "generate_candidate",
    "reissue_edit_prompt",
    "render_call_echo",
    "verify_edit_candidate",
    "verify_tool_candidate",
]

#: The only edit formats a pure pre-check can verify: the deterministic builtin
#: appliers (mirrors ``tools.fs_write.EDIT_FORMATS`` — kept local on purpose so
#: plugin-registered formats (MS-5) never LOOK verifiable here).
_BUILTIN_APPLIERS = {
    "unified_diff": apply_unified_diff,
    "search_replace": apply_search_replace,
    "whole_file": apply_whole_file,
}


@dataclass
class Candidate:
    """One resampled completion, parsed into tool calls (or a failure reason).

    ``calls`` is non-empty exactly when ``error`` is ``None``. ``tokens`` is the
    provider-reported total token cost of generating this candidate (0 when the
    server reports no usage) — the engine charges it to the turn budget.
    """

    calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    tokens: int = 0
    error: str | None = None


async def generate_candidate(
    provider: Provider,
    messages: list[Message],
    *,
    protocol: str,
    tool_specs: list[dict] | None = None,
    sampling: SamplingPolicy | None = None,
    response_format: dict | None = None,
) -> Candidate:
    """Generate ONE candidate: a single non-streaming completion, parsed per rung.

    ``protocol`` is the active tool-call rung (``native`` / ``strict_json`` /
    ``text_protocol``); ``response_format`` must be forwarded on the guided rung
    so candidates stay server-constrained. Never raises for model/provider
    failures: a transport error, a malformed body, a ``done`` finish, or a
    call-free reply all come back as ``Candidate(error=..., calls=[])``.
    """
    kwargs: dict = {"tools": tool_specs, "sampling": sampling}
    if response_format is not None:
        kwargs["response_format"] = response_format
    try:
        result = await provider.complete(messages, **kwargs)
    except ProviderError as exc:
        return Candidate(error=f"candidate generation failed: {exc}")

    text = result.message.content or ""
    tokens = int(result.usage.get("total_tokens", 0))

    if protocol == "text_protocol":
        parsed = ironcall.parse(text)
        if parsed.error is not None:
            return Candidate(text=text, tokens=tokens, error=parsed.error)
        calls = list(parsed.calls)
    elif protocol == "strict_json":
        gparsed = guided.parse_guided_tool_call(text)
        if gparsed.error is not None:
            return Candidate(text=text, tokens=tokens, error=gparsed.error)
        if gparsed.call is None:  # `done` (or an empty parse): no call to race
            return Candidate(
                text=text,
                tokens=tokens,
                error="the candidate finished instead of issuing a tool call",
            )
        calls = [gparsed.call]
    else:  # native function-calling
        calls = list(result.message.tool_calls)

    if not calls:
        return Candidate(
            text=text, tokens=tokens, error="the candidate reply contained no tool call"
        )
    return Candidate(calls=calls, text=text, tokens=tokens)


def verify_tool_candidate(candidate: Candidate, known_tools: set[str]) -> bool:
    """Parse-seam verifier: the candidate parsed cleanly AND every call names a
    registered tool. Pure; the safety gate still decides whether anything runs."""
    if candidate.error is not None or not candidate.calls:
        return False
    return all(call.name in known_tools for call in candidate.calls)


def verify_edit_candidate(
    candidate: Candidate,
    current_text: str,
    *,
    expected_path: str | None = None,
) -> tuple[bool, str]:
    """Edit-seam verifier: does the candidate's patch APPLY, in memory, now?

    Passes only when the candidate is exactly one ``edit_file`` call, on
    ``expected_path`` (when given — a candidate that wanders to another file was
    not verified against its actual target), in a BUILT-IN format, whose pure
    applier succeeds against ``current_text``. Nothing is written; the winner
    still executes through the real tool + gate, which re-applies on disk.
    """
    if candidate.error is not None:
        return False, candidate.error
    if len(candidate.calls) != 1:
        return False, f"expected exactly one edit_file call, got {len(candidate.calls)}"
    call = candidate.calls[0]
    if call.name != "edit_file":
        return False, f"expected an edit_file call, got {call.name!r}"
    path = call.arguments.get("path")
    if expected_path is not None and path != expected_path:
        return False, f"the candidate edits {path!r}, not the failing file {expected_path!r}"
    fmt = call.arguments.get("format")
    applier = _BUILTIN_APPLIERS.get(fmt)  # type: ignore[arg-type]
    if applier is None:
        return False, f"format {fmt!r} is not a built-in edit format; cannot pre-verify"
    edit = call.arguments.get("edit")
    if not isinstance(edit, str):
        return False, "the candidate call carries no 'edit' string"
    result = applier(current_text, edit)
    if not result.ok:
        return False, result.reason or "the edit does not apply"
    return True, ""


def reissue_edit_prompt(call: ToolCall, reason: str) -> str:
    """Deterministic re-ask instruction for a mechanically-failed edit.

    Names the path, the format, and the mechanical reason, and demands ONE fresh
    ``edit_file`` call regenerated from the current file contents — the same
    framing discipline as ``repair.frame_error``. Pure and reproducible.
    """
    path = call.arguments.get("path", "")
    fmt = call.arguments.get("format", "")
    why = (reason or "").strip() or "it could not be applied"
    return (
        f"Your edit_file call on {path!r} (format={fmt!r}) failed mechanically: {why}\n"
        "Re-issue EXACTLY ONE corrected edit_file call for the SAME file. Regenerate "
        "the edit payload from the file's CURRENT contents; do not repeat the failing "
        "payload."
    )


def render_call_echo(call: ToolCall, protocol: str) -> str:
    """The failing call re-rendered in its rung's own in-band wire form, for the
    assistant-echo message of a resample re-ask. Native returns ``""`` — the
    echo ``Message`` carries the call in ``tool_calls`` instead."""
    if protocol == "native":
        return ""
    body = json.dumps({"tool": call.name, "args": call.arguments}, ensure_ascii=False)
    if protocol == "text_protocol":
        return f"```ironcall\n{body}\n```"
    return body  # strict_json: the bare guided object
