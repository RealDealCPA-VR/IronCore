"""Guided decoding — the strict_json rung of the tool-call ladder (SPEC §6,
CONTRACTS §2; docs/plans/guided-decoding.md).

The middle rung between native function-calling and the IRONCALL text floor.
When the envelope routes a mid-tier model to ``strict_json``, the engine
*constrains the server's generation* to a JSON schema so the model emits a
guaranteed well-formed tool call — one object, one call:

    {"tool": "read_file", "args": {"path": "src/app.py"}}

and finishes the turn with the ``done`` pseudo-tool:

    {"tool": "done", "args": {"message": "Read the file and fixed the bug."}}

This module is the pure helper that path uses. It has three jobs:

- ``tool_call_response_format(specs)`` builds the OpenAI *structured outputs*
  object (``{"type":"json_schema", ...}``) whose schema pins output to one call
  — a ``tool`` enum of every tool NAME plus ``"done"``, and an ``args`` object.
  That enum makes a malformed tool name impossible on any guided backend.
- ``render_json_system_fragment(specs)`` is the system-prompt text that teaches
  the model the object shape (the schema carries only names, so the prose still
  needs the tool docs), few-shot like IRONCALL.
- ``parse_guided_tool_call(text)`` decodes the constrained reply into a
  ``GuidedParse``: a real call, a ``done`` finish, or — for a server that
  ignored ``response_format`` — a precise, model-facing repair string. It never
  raises; malformed output is *repairable data*, exactly like the IRONCALL floor.

Determinism; stdlib only (``json``, ``dataclasses``) plus the ``ToolCall`` wire
type. No engine, tool, or provider imports beyond that type.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ironcore.providers.base import ToolCall

__all__ = [
    "GuidedParse",
    "parse_guided_tool_call",
    "render_json_system_fragment",
    "tool_call_response_format",
]

#: The pseudo-tool that lets a fully-constrained model stop. Because
#: ``response_format`` forces JSON on every turn the model cannot emit free text
#: to finish, so ``{"tool":"done","args":{"message":...}}`` ends the turn.
DONE = "done"

#: Appended to every parser error so the repair loop always shows the model a
#: concrete, copyable template of both a call and a finish.
_HINT = (
    'emit exactly one JSON object like {"tool":"read_file","args":{"path":"x"}} '
    'or, to finish, {"tool":"done","args":{"message":"<summary>"}}'
)


@dataclass
class GuidedParse:
    """Outcome of parsing one guided (strict_json) model reply.

    - ``call``: the parsed tool call, or ``None`` when the model finished
      (``done``) or the body was unusable.
    - ``done``: ``True`` when the model emitted the ``done`` pseudo-tool to end
      the turn; ``call`` is ``None`` in that case.
    - ``message``: the ``done`` summary shown to the user; ``""`` otherwise.
    - ``text``: the raw model reply, preserved verbatim (even on error).
    - ``error``: a precise, model-facing repair message when the body was not a
      usable tool-call object; ``None`` on a clean parse (call *or* done).
    """

    call: ToolCall | None = None
    done: bool = False
    message: str = ""
    text: str = ""
    error: str | None = None


def tool_call_response_format(tools: list[dict]) -> dict:
    """Build the OpenAI structured-outputs ``response_format`` that constrains a
    reply to exactly one tool call.

    ``tools`` is the ``ToolRegistry.specs()`` list (each ``{"type":"function",
    "function":{"name",...}}``); names are read from ``t["function"]["name"]``
    (a flat ``{"name":...}`` dict is also tolerated). The schema's ``tool`` enum
    is every tool name plus ``"done"`` — so the model can only ever name a real
    tool or finish. Empty ``tools`` yields an enum of just ``["done"]``.
    """
    names = _tool_names(tools)
    enum = [*names, DONE] if DONE not in names else list(names)
    schema = {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": enum},
            "args": {"type": "object"},
        },
        "required": ["tool", "args"],
        "additionalProperties": False,
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "ironcore_tool_call",
            "strict": True,
            "schema": schema,
        },
    }


def render_json_system_fragment(tools: list[dict]) -> str:
    """System-prompt text that teaches a model the guided JSON protocol.

    Three parts, kept compact: a one-paragraph rule, a rendered tool catalog
    (name + description + params, from ``ToolRegistry.specs()`` function specs),
    and two worked examples — one real call and one ``done`` finish — because
    few-shot examples beat instructions at this model scale. ASCII-safe.
    """
    parts: list[str] = [
        "# Using tools (guided JSON)\n"
        "Reply with EXACTLY ONE JSON object and nothing else: no prose, no code "
        'fences. To call a tool, emit `{"tool": "<name>", "args": {<arguments>}}` '
        "naming one tool from the list below. You will receive the result as a "
        "JSON message; read it, then emit the next object. Use `{}` for a tool "
        "that takes no arguments, and call one tool at a time. When the task is "
        'complete, finish by emitting `{"tool": "done", "args": {"message": '
        '"<short summary>"}}`, which ends the turn and shows your summary.',
        "## Tools you can call",
    ]

    if tools:
        parts.extend(_render_tool(spec) for spec in tools)
    else:
        parts.append("(no tools are available this turn)")

    parts.append("## Examples")
    parts.append(
        "A tool call - read a file:\n"
        '{"tool": "read_file", "args": {"path": "src/app.py"}}'
    )
    parts.append(
        "Finishing the turn once the work is done:\n"
        '{"tool": "done", "args": {"message": "Read the file and fixed the bug."}}'
    )
    return "\n\n".join(parts)


def parse_guided_tool_call(text: str) -> GuidedParse:
    """Decode one guided (strict_json) reply into a ``GuidedParse``. Never raises.

    With ``response_format`` in force the server emits pure JSON, so a bare
    ``json.loads`` is the common path; a first-``{`` to last-``}`` slice tolerates
    a server that wrapped the object in stray prose. A ``done`` object finishes
    the turn; any other well-formed ``{"tool","args"}`` object becomes a
    ``ToolCall`` (stable id ``gd-0``); anything else becomes a precise,
    repairable ``error`` string — never an exception.
    """
    payload = _load_object(text)
    if payload is None:
        return GuidedParse(
            text=text,
            error=f"your reply was not a valid tool-call JSON object; {_HINT}",
        )

    tool = payload.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        return GuidedParse(
            text=text,
            error=(
                'your JSON object must carry a string "tool" naming one listed '
                f'tool or "done"; {_HINT}'
            ),
        )

    if tool == DONE:
        args = payload.get("args")
        message = args.get("message", "") if isinstance(args, dict) else ""
        if not isinstance(message, str):
            message = str(message)
        return GuidedParse(done=True, message=message, text=text)

    args = payload.get("args")
    if not isinstance(args, dict):
        return GuidedParse(
            text=text,
            error=(
                'your JSON object must carry an "args" object (use {} for no '
                f"arguments); {_HINT}"
            ),
        )

    return GuidedParse(call=ToolCall(id="gd-0", name=tool, arguments=args), text=text)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _tool_names(tools: list[dict]) -> list[str]:
    """Unique tool names, in order, from ``ToolRegistry.specs()`` function specs.
    Tolerant of a flat ``{"name": ...}`` dict and of junk entries."""
    names: list[str] = []
    for spec in tools:
        if not isinstance(spec, dict):
            continue
        fn = spec.get("function", spec)
        name = fn.get("name") if isinstance(fn, dict) else None
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _load_object(text: str) -> dict | None:
    """Best-effort decode of a model reply to a JSON object; ``None`` on failure
    (never raises). Tries a bare load first (the constrained path), then a
    first-``{`` to last-``}`` slice so a single object wrapped in prose is still
    recovered. Multiple objects or genuinely malformed input yield ``None`` — a
    clean error, not a wrong guess."""
    stripped = text.strip()
    obj = _try_load(stripped)
    if isinstance(obj, dict):
        return obj
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        obj = _try_load(stripped[start : end + 1])
        if isinstance(obj, dict):
            return obj
    return None


def _try_load(candidate: str) -> object | None:
    """``json.loads`` that returns ``None`` instead of raising."""
    try:
        return json.loads(candidate)
    except ValueError:  # json.JSONDecodeError is a ValueError subclass
        return None


def _render_tool(spec: dict) -> str:
    """One compact catalog entry from an OpenAI function spec (the shape
    ``ToolRegistry.specs()`` emits). Tolerant of a flat ``{name, ...}`` dict
    and of junk entries (mirrors the schema builder's guards)."""
    fn = spec.get("function", spec) if isinstance(spec, dict) else {}
    if not isinstance(fn, dict):
        fn = {}
    name = fn.get("name", "?")
    description = (fn.get("description") or "").strip()
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    required = set(params.get("required") or [])

    rendered: list[str] = []
    for arg_name, arg_schema in props.items():
        arg_type = (arg_schema or {}).get("type", "any")
        tag = f"{arg_name}: {arg_type}"
        if arg_name in required:
            tag += ", required"
        rendered.append(tag)
    args_line = "; ".join(rendered) if rendered else "none"

    header = f"- `{name}` - {description}" if description else f"- `{name}`"
    return f"{header}\n    args: {args_line}"
