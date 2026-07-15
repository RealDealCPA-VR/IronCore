"""/loop (IC-804): run a prompt on a fixed interval or self-paced.

    /loop <interval> <prompt>  — recurring, e.g. ``/loop 5m check the build``
    /loop <prompt>             — self-paced (no fixed interval)
    /loop status               — show the active loop
    /loop stop                 — cancel it

Intervals are ``30s`` / ``5m`` / ``1h`` / ``2d`` or a bare number of seconds.
The recurring EXECUTION is the app's job (the handler is sync and must not
block); registration stores a :class:`LoopSpec` in a module-level map keyed by
workspace and, when the live app exposes a ``register_loop`` hook, hands the
spec off to it. Without an app the spec is stored and acknowledged — and
:func:`parse_interval`, :class:`LoopSpec`, and the map are all exercised
directly by the tests, no TUI required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ironcore.commands._helpers import resolve_workspace
from ironcore.commands.base import CommandContext, SlashCommand

_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_INTERVAL_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])$", re.IGNORECASE)

#: workspace-key -> the one active loop for that workspace.
_LOOPS: dict[str, LoopSpec] = {}


def parse_interval(text: str) -> float | None:
    """Parse ``30s`` / ``5m`` / ``1h`` / ``2d`` or a bare number → seconds.

    Returns ``None`` for anything that is not a positive interval, so callers
    can cleanly distinguish "no interval given" (self-paced) from a real value.
    """
    text = text.strip().lower()
    if not text:
        return None
    match = _INTERVAL_RE.match(text)
    if match:
        value = float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
    else:
        try:
            value = float(text)
        except ValueError:
            return None
    return value if value > 0 else None


def _format_interval(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s" if seconds == int(seconds) else f"{seconds:g}s"


@dataclass
class LoopSpec:
    """A registered recurring prompt. ``interval_s`` is ``None`` when self-paced."""

    prompt: str
    interval_s: float | None = None

    def describe(self) -> str:
        if self.interval_s is None:
            cadence = "self-paced"
        else:
            cadence = f"every {_format_interval(self.interval_s)}"
        return f"{cadence}: {self.prompt}"


def _key(ctx: CommandContext) -> str:
    ws = resolve_workspace(ctx)
    return str(ws) if ws is not None else "<no-workspace>"


def _cmd_loop(ctx: CommandContext, args: str) -> str:
    args = args.strip()
    key = _key(ctx)

    if not args or args.lower() == "status":
        spec = _LOOPS.get(key)
        if spec is None:
            return "No loop running. Usage: /loop [interval] <prompt>"
        return f"Loop: {spec.describe()}"

    if args.lower() == "stop":
        removed = _LOOPS.pop(key, None)
        app = ctx.extra.get("app")
        if app is not None and hasattr(app, "stop_loop"):
            try:
                app.stop_loop()
            except Exception:  # noqa: BLE001 — app hook failure must not crash the command
                pass
        return f"Loop stopped: {removed.prompt}" if removed is not None else "No loop to stop."

    # Register: an optional leading interval token, then the prompt.
    first, _, rest = args.partition(" ")
    interval = parse_interval(first)
    if interval is not None and rest.strip():
        prompt = rest.strip()
    else:
        interval, prompt = None, args
    spec = LoopSpec(prompt=prompt, interval_s=interval)
    _LOOPS[key] = spec

    started = False
    app = ctx.extra.get("app")
    if app is not None and hasattr(app, "register_loop"):
        try:
            app.register_loop(spec)
            started = True
        except Exception:  # noqa: BLE001 — degrade to stored-only on any app-side error
            started = False
    tail = "" if started else " (stored; runs when the session drives it)"
    return f"Loop registered — {spec.describe()}.{tail}\nStop with /loop stop."


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        "loop",
        "run a prompt on an interval or self-paced",
        "/loop [interval] <prompt> | status | stop",
        _cmd_loop,
    ),
)
