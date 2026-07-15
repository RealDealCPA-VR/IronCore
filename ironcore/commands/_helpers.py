"""Shared context accessors for the phase-8 slash-command handlers.

Handlers read their dependencies out of ``ctx.extra`` (the TUI populates
``app``/``engine``/``registry``/``workspace``/``provider_registry``/``settings``/
``schedule`` — see docs/ARCHITECTURE.md §6). Every key is OPTIONAL: headless use
and unit tests hand-build a ``CommandContext`` with only what a given command
needs, so every accessor tolerates a missing key and returns ``None`` rather
than raising. Nothing here imports the TUI (dependency rule: nothing imports
``tui/``); the engine/provider objects are duck-typed on purpose.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # annotations only — keep this module import-light
    from ironcore.commands.base import CommandContext


def resolve_workspace(ctx: CommandContext) -> Path | None:
    """The workspace ``Path`` for filesystem/snapshot work.

    Prefers the explicit ``workspace`` key, then the live engine's workspace.
    Returns ``None`` when neither is available (the handler then reports that
    the operation needs a live session).
    """
    ws = ctx.extra.get("workspace")
    if ws is None:
        engine = ctx.extra.get("engine")
        ws = getattr(engine, "workspace", None) if engine is not None else None
    if ws is None:
        return None
    return Path(ws)


def resolve_provider(ctx: CommandContext, role: str = "verifier") -> Any | None:
    """A live ``Provider`` for a role, or ``None`` if none is reachable.

    Tries ``provider_registry.for_role(role)`` (the config-routed model for that
    role), falls back to the registry default, then to the engine's own
    provider. All failures degrade to ``None`` so a command can report cleanly.
    """
    reg = ctx.extra.get("provider_registry")
    if reg is not None:
        try:
            return reg.for_role(role)
        except Exception:  # noqa: BLE001 — unknown role / closed registry → fall back
            try:
                return reg.default
            except Exception:  # noqa: BLE001 — closed registry → fall back to engine
                pass
    engine = ctx.extra.get("engine")
    return getattr(engine, "provider", None) if engine is not None else None
