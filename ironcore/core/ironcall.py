"""IRONCALL — the floor tool-call text protocol (SPEC §6.3, CONTRACTS §10).

The last rung of the tool-call ladder (envelope §4.3), for models without
reliable native function-calling or strict-JSON output. A model asks to use a
tool by emitting a single fenced code block:

    ```ironcall
    {"tool": "read_file", "args": {"path": "src/app.py"}}
    ```

and the engine feeds the outcome back as a fenced ``ironresult`` block. The
whole protocol is regex + ``json.loads`` — it is the always-works floor
precisely because parsing never depends on model goodwill.

Parser rules (frozen once IC-606 lands):

- Blocks are found by fence regex; the language tag tolerates ``ironcall`` or
  ``iron_call`` (any case), leading/trailing spaces on the info line, and
  CRLF or LF framing, with arbitrary prose surrounding the block.
- Each block body is parsed with ``json.loads``. Malformed JSON is *repairable
  data*, never an exception: it becomes a precise, model-facing ``error`` string
  the repair loop (SPEC §5.4) re-presents to the model.
- A body must be a JSON object carrying a non-empty string ``"tool"``; ``"args"``
  must be an object when present (absent ``args`` means "no arguments"). Any
  violation is a precise ``error``, not a raised exception.
- One call per reply is the contract: when a reply contains more than one
  ``ironcall`` block the FIRST call is taken and a non-fatal ``warning`` is set
  (execution still proceeds on that first call).
- Each returned call gets a stable id ``ic-<index>`` (the taken call is ``ic-0``).

Determinism; stdlib only (``re``, ``json``). No engine imports.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ironcore.providers.base import ToolCall

__all__ = ["IroncallParse", "parse", "render_result", "render_system_fragment"]

#: Matches a fenced ironcall block. Tolerates ``ironcall``/``iron_call`` (any
#: case), spaces/tabs on the info line, and CRLF or LF newlines. The body is
#: captured non-greedily so it stops at the first closing fence — a second
#: block therefore becomes a separate match, which drives the multiple-blocks
#: warning rather than swallowing everything up to the final fence.
_BLOCK_RE = re.compile(
    r"```[ \t]*iron_?call[ \t]*\r?\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)

#: Appended to every parser error so the repair loop always shows the model a
#: concrete, copyable template of a valid call.
_HINT = (
    'emit a single fenced block like ```ironcall\\n'
    '{"tool":"read_file","args":{"path":"x"}}\\n```'
)


@dataclass
class IroncallParse:
    """Outcome of parsing one model reply for IRONCALL tool calls.

    - ``calls``: parsed tool calls — 0 or 1. The floor protocol is one call per
      reply; any extra blocks are dropped with a ``warning``.
    - ``text``: the prose with all ironcall blocks removed (what the model
      "said" around the call). Preserved even on error.
    - ``error``: a precise, model-facing message for the repair loop when a
      block was present but unusable; ``None`` when parsing was clean, and also
      ``None`` when there was simply no block to parse (a pure-prose reply).
    - ``warning``: non-fatal note (e.g. more than one block) surfaced to the
      transcript; it never blocks execution of the taken call.
    """

    calls: list[ToolCall] = field(default_factory=list)
    text: str = ""
    error: str | None = None
    warning: str | None = None


def parse(text: str) -> IroncallParse:
    """Extract IRONCALL tool calls from a model reply. Never raises.

    Returns at most one call (the first ``ironcall`` block). Malformed input is
    surfaced as ``error``/``warning`` strings written for the model, not as
    exceptions — the whole point of the floor protocol.
    """
    prose = _strip_blocks(text)
    bodies = _BLOCK_RE.findall(text)

    if not bodies:
        # No tool call requested — the model just talked. Not an error.
        return IroncallParse(calls=[], text=prose, error=None)

    warning: str | None = None
    if len(bodies) > 1:
        warning = (
            f"{len(bodies)} ironcall blocks were found in one reply; only the "
            "FIRST was taken. Emit exactly ONE ironcall block per turn."
        )

    body = bodies[0].strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return IroncallParse(
            calls=[],
            text=prose,
            error=(
                f"your ironcall block was not valid JSON: {exc.msg} "
                f"(line {exc.lineno} col {exc.colno}); {_HINT}"
            ),
            warning=warning,
        )

    error = _validate(payload)
    if error is not None:
        return IroncallParse(calls=[], text=prose, error=error, warning=warning)

    call = ToolCall(id="ic-0", name=payload["tool"], arguments=payload.get("args", {}))
    return IroncallParse(calls=[call], text=prose, error=None, warning=warning)


def render_result(tool_call_id: str, output: str, ok: bool) -> str:
    """Render the ``ironresult`` block the engine feeds back after running a
    call. The body is JSON — with the call id, the ok flag, and the (already
    truncated/redacted) output — so both the model and this parser can read it
    back. ``json.dumps`` guarantees the block is parse-able."""
    body = json.dumps(
        {"id": tool_call_id, "ok": ok, "output": output},
        ensure_ascii=False,
    )
    return f"```ironresult\n{body}\n```"


def render_system_fragment(tools: list[dict]) -> str:
    """System-prompt text that teaches a weak model the IRONCALL protocol.

    Three parts, kept compact: a one-paragraph rule, a rendered tool catalog
    (name + description + params, from ``ToolRegistry.specs()`` function specs),
    and two worked examples — one call with arguments and one without, because
    few-shot examples beat instructions at this model scale.
    """
    parts: list[str] = [
        "# Using tools (IRONCALL)\n"
        "To use a tool, emit EXACTLY ONE fenced code block tagged `ironcall` "
        "containing a single JSON object of the form "
        '`{"tool": "<name>", "args": {<arguments>}}`. Put nothing after the '
        "block. You will then receive the result in a fenced `ironresult` "
        "block — read it, then continue. Use `{}` for a tool that takes no "
        "arguments. Call one tool at a time.",
        "## Tools you can call",
    ]

    if tools:
        parts.extend(_render_tool(spec) for spec in tools)
    else:
        parts.append("(no tools are available this turn)")

    parts.append("## Examples")
    parts.append(
        "A call with arguments — read a file:\n"
        "```ironcall\n"
        '{"tool": "read_file", "args": {"path": "src/app.py"}}\n'
        "```"
    )
    parts.append(
        "A call with no arguments — list the working directory:\n"
        "```ironcall\n"
        '{"tool": "list_dir", "args": {}}\n'
        "```"
    )
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _strip_blocks(text: str) -> str:
    """Remove every ironcall block and tidy the leftover prose."""
    prose = _BLOCK_RE.sub("", text)
    prose = re.sub(r"\n{3,}", "\n\n", prose)  # collapse gaps left by removal
    return prose.strip()


def _validate(payload: object) -> str | None:
    """Return a precise, model-facing error for a decoded body, or ``None`` when
    the body is a well-formed ironcall call."""
    if not isinstance(payload, dict):
        return (
            "your ironcall body must be a JSON object "
            f'{{"tool": ..., "args": {{...}}}}, not a {type(payload).__name__}; '
            f"{_HINT}"
        )
    if "tool" not in payload:
        return f'your ironcall block is missing the required "tool" key; {_HINT}'
    if not isinstance(payload["tool"], str) or not payload["tool"].strip():
        return (
            'your ironcall "tool" must be a non-empty string naming one tool; '
            f"{_HINT}"
        )
    if "args" in payload and not isinstance(payload["args"], dict):
        return (
            'your ironcall "args" must be a JSON object (use {} for no '
            f"arguments), not a {type(payload['args']).__name__}; {_HINT}"
        )
    return None


def _render_tool(spec: dict) -> str:
    """One compact catalog entry from an OpenAI function spec (the shape
    ``ToolRegistry.specs()`` emits). Tolerant of a flat ``{name, ...}`` dict."""
    fn = spec.get("function", spec)
    name = fn.get("name", "?")
    description = (fn.get("description") or "").strip()
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    required = set(params.get("required") or [])

    rendered: list[str] = []
    for arg_name, schema in props.items():
        arg_type = (schema or {}).get("type", "any")
        tag = f"{arg_name}: {arg_type}"
        if arg_name in required:
            tag += ", required"
        rendered.append(tag)
    args_line = "; ".join(rendered) if rendered else "none"

    header = f"- `{name}` — {description}" if description else f"- `{name}`"
    return f"{header}\n    args: {args_line}"
