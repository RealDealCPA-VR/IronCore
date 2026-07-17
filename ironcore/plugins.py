"""Entry-point plugin discovery (MS-5): extend IronCore without touching core.

Installed distributions contribute five kinds of extension via standard Python
entry points (``[project.entry-points."<group>"]`` in their pyproject.toml —
docs/PLUGINS.md is the author guide, CONTRACTS §11 freezes the surfaces):

    ironcore.tools         factory(settings, workspace) -> Tool | Sequence[Tool]
    ironcore.commands      SlashCommand | Sequence[SlashCommand] (the COMMANDS
                           tuple convention — the entry point IS the object)
    ironcore.probes        zero-arg factory -> Probe | Sequence[Probe]
    ironcore.providers     factory(base_url=, api_key=, model=[, transport=])
                           -> Provider, selected when ``provider.type`` equals
                           the entry-point name
    ironcore.edit_formats  apply(original_text, edit) -> PatchResult, keyed by
                           the entry-point name

RULES
-----
- Fail-safe: a broken plugin is SKIPPED and recorded (``doctor`` lists each
  skip), never a crash. ``ep.load()``, factory calls, and validation each sit
  in their own try/except; even a broken metadata backend only yields an
  empty load. Order is deterministic: entry points process sorted by
  ``(name, value)``.
- The safety kernel is NOT extensible: a plugin tool must carry a real
  ``ToolRisk`` and passes the same ``decide(mode, risk)`` gate as builtins;
  NET-risk plugin tools are not even loaded unless ``safety.network_tools``
  (the ``fetch_url`` rule). Installation is the consent moment — pip install
  already executed arbitrary code (docs/SAFETY.md T9); ``[plugins]
  enabled = false`` disables discovery entirely for hardened setups.
- Builtins win every duplicate-name clash downstream (tools, commands, edit
  formats); reserved names (builtin probe ids, auto/ollama/openai provider
  types, the builtin edit-format ladder) are refused here.
- This module may import anything except ``tui/``; layered packages never
  import it at runtime — registries take an already-loaded ``plugins=`` value.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from importlib import metadata
from typing import TYPE_CHECKING, Any

from ironcore.commands.base import SlashCommand
from ironcore.config.settings import Settings
from ironcore.safety.risk import ToolRisk
from ironcore.tools.base import Tool
from ironcore.tools.fs_write import EDIT_FORMATS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

GROUP_TOOLS = "ironcore.tools"
GROUP_COMMANDS = "ironcore.commands"
GROUP_PROBES = "ironcore.probes"
GROUP_PROVIDERS = "ironcore.providers"
GROUP_EDIT_FORMATS = "ironcore.edit_formats"
_GROUPS = (GROUP_TOOLS, GROUP_COMMANDS, GROUP_PROBES, GROUP_PROVIDERS, GROUP_EDIT_FORMATS)

#: ``provider.type`` values owned by the built-in selection
#: (``providers.registry.select_provider_factory``) — a plugin may not shadow them.
RESERVED_PROVIDER_TYPES = frozenset({"auto", "ollama", "openai"})

#: The built-in probe battery's ids (``envelope.suite.default_probe_suite``).
#: A plugin probe reusing one would make failure attribution ambiguous.
RESERVED_PROBE_IDS = frozenset(
    {
        "CTX-HONESTY",
        "RETENTION",
        "TOOL-FORM",
        "JSON-STRICT",
        "EDIT-FORMAT",
        "CODE-SMOKE",
        "TOKEN-RATIO",
    }
)

#: Edit-format names: a lowercase slug so the model can type it into the
#: ``format`` enum; the builtin ladder rungs are checked separately.
_FORMAT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class SkippedPlugin:
    """One plugin (or one produced object) that did not load, and why."""

    group: str
    name: str
    reason: str


@dataclass
class LoadedPlugins:
    """Everything discovery produced, ready to thread into the registries.

    ``skipped`` also collects DOWNSTREAM duplicate-name skips (via
    ``note_skip``) so ``doctor``/boot notes can show one honest list.
    """

    tools: list[Tool] = field(default_factory=list)
    commands: list[SlashCommand] = field(default_factory=list)
    probes: list[Any] = field(default_factory=list)
    provider_factories: dict[str, Callable[..., Any]] = field(default_factory=dict)
    edit_formats: dict[str, Callable[[str, str], Any]] = field(default_factory=dict)
    skipped: list[SkippedPlugin] = field(default_factory=list)

    @classmethod
    def empty(cls) -> LoadedPlugins:
        return cls()

    def note_skip(self, group: str, name: str, reason: str) -> None:
        self.skipped.append(SkippedPlugin(group, name, reason))

    def summary(self) -> str:
        """Human counts for doctor/boot notes, e.g. ``2 tools, 1 edit format``."""
        counts = (
            (len(self.tools), "tool", "tools"),
            (len(self.commands), "command", "commands"),
            (len(self.probes), "probe", "probes"),
            (len(self.provider_factories), "provider factory", "provider factories"),
            (len(self.edit_formats), "edit format", "edit formats"),
        )
        parts = [f"{n} {one if n == 1 else many}" for n, one, many in counts if n]
        return ", ".join(parts) if parts else "none loaded"


def load_plugins(
    settings: Settings,
    workspace: Path,
    *,
    entry_points_fn: Callable[..., Iterable[metadata.EntryPoint]] = metadata.entry_points,
) -> LoadedPlugins:
    """Discover and validate every installed plugin. Never raises.

    ``entry_points_fn`` is the injectable test seam (called as
    ``entry_points_fn(group=...)`` exactly like ``importlib.metadata``).
    ``settings.plugins.enabled = False`` short-circuits without touching it.
    """
    loaded = LoadedPlugins()
    if not settings.plugins.enabled:
        return loaded
    for ep in _discover(entry_points_fn, GROUP_TOOLS, loaded):
        _load_tools(ep, settings, workspace, loaded)
    for ep in _discover(entry_points_fn, GROUP_COMMANDS, loaded):
        _load_commands(ep, loaded)
    for ep in _discover(entry_points_fn, GROUP_PROBES, loaded):
        _load_probes(ep, loaded)
    for ep in _discover(entry_points_fn, GROUP_PROVIDERS, loaded):
        _load_provider(ep, loaded)
    for ep in _discover(entry_points_fn, GROUP_EDIT_FORMATS, loaded):
        _load_edit_format(ep, loaded)
    return loaded


# --------------------------------------------------------------------------- #
# discovery + shared plumbing
# --------------------------------------------------------------------------- #


def _discover(
    entry_points_fn: Callable[..., Iterable[metadata.EntryPoint]],
    group: str,
    loaded: LoadedPlugins,
) -> list[metadata.EntryPoint]:
    try:
        eps = list(entry_points_fn(group=group))
    except Exception as exc:  # noqa: BLE001 — a broken metadata backend must not crash boot
        loaded.note_skip(group, "*", f"entry-point discovery failed: {exc}")
        return []
    return sorted(eps, key=lambda ep: (ep.name, ep.value))


def _as_sequence(produced: Any) -> list[Any]:
    """A factory may return one object or a list/tuple of them."""
    if isinstance(produced, (list, tuple)):
        return list(produced)
    return [produced]


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# per-group loaders (each failure -> SkippedPlugin, never a raise)
# --------------------------------------------------------------------------- #


def _load_tools(
    ep: metadata.EntryPoint, settings: Settings, workspace: Path, loaded: LoadedPlugins
) -> None:
    try:
        factory = ep.load()
        produced = factory(settings, workspace)
    except Exception as exc:  # noqa: BLE001 — plugin defects are skips, not crashes
        loaded.note_skip(GROUP_TOOLS, ep.name, _error(exc))
        return
    for tool in _as_sequence(produced):
        reason = _validate_tool(tool, settings)
        if reason is not None:
            loaded.note_skip(GROUP_TOOLS, ep.name, reason)
            continue
        loaded.tools.append(tool)


def _validate_tool(tool: Any, settings: Settings) -> str | None:
    """None = valid; otherwise the skip reason. The risk check is strict —
    the gate governs by declared risk, so a non-ToolRisk value is refused."""
    if not isinstance(tool, Tool):
        return f"factory returned {type(tool).__name__}, not a Tool"
    name = getattr(tool, "name", None)
    if not isinstance(name, str) or not name:
        return "tool has no nonempty string name"
    if type(getattr(tool, "risk", None)) is not ToolRisk:
        return f"tool {name!r} must declare risk as a ToolRisk member"
    if not isinstance(getattr(tool, "parameters", None), dict):
        return f"tool {name!r} parameters must be a JSON-schema dict"
    try:
        tool.spec()
    except Exception as exc:  # noqa: BLE001 — a broken spec() must not crash boot
        return f"tool {name!r} spec() raised {_error(exc)}"
    if tool.risk is ToolRisk.NET and not settings.safety.network_tools:
        return (
            f"tool {name!r} is NET-risk and [safety] network_tools is false "
            "(an off NET tool is never registered)"
        )
    return None


def _load_commands(ep: metadata.EntryPoint, loaded: LoadedPlugins) -> None:
    try:
        produced = ep.load()
    except Exception as exc:  # noqa: BLE001
        loaded.note_skip(GROUP_COMMANDS, ep.name, _error(exc))
        return
    for command in _as_sequence(produced):
        if not isinstance(command, SlashCommand):
            loaded.note_skip(
                GROUP_COMMANDS,
                ep.name,
                f"entry point yielded {type(command).__name__}, not a SlashCommand",
            )
            continue
        if not callable(command.handler):
            loaded.note_skip(
                GROUP_COMMANDS, ep.name, f"command /{command.name} handler is not callable"
            )
            continue
        loaded.commands.append(command)


def _load_probes(ep: metadata.EntryPoint, loaded: LoadedPlugins) -> None:
    try:
        factory = ep.load()
        produced = factory()
    except Exception as exc:  # noqa: BLE001
        loaded.note_skip(GROUP_PROBES, ep.name, _error(exc))
        return
    for probe in _as_sequence(produced):
        reason = _validate_probe(probe)
        if reason is not None:
            loaded.note_skip(GROUP_PROBES, ep.name, reason)
            continue
        loaded.probes.append(probe)


def _validate_probe(probe: Any) -> str | None:
    """Duck-typed against envelope.runner.Probe — probes only FILL profile
    fields via the runner's dotted-path merge; selection stays recommended_*."""
    pid = getattr(probe, "id", None)
    if not isinstance(pid, str) or not pid:
        return "probe has no nonempty string id"
    if pid in RESERVED_PROBE_IDS:
        return f"probe id {pid!r} is reserved by the built-in battery"
    if not isinstance(getattr(probe, "title", None), str):
        return f"probe {pid!r} has no string title"
    targets = getattr(probe, "targets", None)
    if (
        isinstance(targets, str)
        or not isinstance(targets, (list, tuple))
        or not all(isinstance(t, str) for t in targets)
    ):
        return f"probe {pid!r} targets must be a list/tuple of dotted profile paths"
    if not inspect.iscoroutinefunction(getattr(probe, "run", None)):
        return f"probe {pid!r} run must be an async method taking a provider"
    return None


def _load_provider(ep: metadata.EntryPoint, loaded: LoadedPlugins) -> None:
    if ep.name in RESERVED_PROVIDER_TYPES:
        loaded.note_skip(
            GROUP_PROVIDERS,
            ep.name,
            f"provider type {ep.name!r} is reserved by the built-in selection",
        )
        return
    if ep.name in loaded.provider_factories:
        loaded.note_skip(GROUP_PROVIDERS, ep.name, "duplicate provider type; first wins")
        return
    try:
        factory = ep.load()
    except Exception as exc:  # noqa: BLE001
        loaded.note_skip(GROUP_PROVIDERS, ep.name, _error(exc))
        return
    if not callable(factory):
        loaded.note_skip(GROUP_PROVIDERS, ep.name, "entry point is not a callable factory")
        return
    # Called later by ProviderRegistry._build as factory(base_url=, api_key=,
    # model=[, transport=]) — the one build path for_role/for_model share.
    loaded.provider_factories[ep.name] = factory


def _load_edit_format(ep: metadata.EntryPoint, loaded: LoadedPlugins) -> None:
    name = ep.name
    if not _FORMAT_NAME_RE.fullmatch(name):
        loaded.note_skip(
            GROUP_EDIT_FORMATS, name, "format name must match ^[a-z][a-z0-9_-]{0,31}$"
        )
        return
    if name in EDIT_FORMATS:
        loaded.note_skip(
            GROUP_EDIT_FORMATS, name, f"format {name!r} is a built-in ladder rung; built-ins win"
        )
        return
    if name in loaded.edit_formats:
        loaded.note_skip(GROUP_EDIT_FORMATS, name, "duplicate format name; first wins")
        return
    try:
        applier = ep.load()
    except Exception as exc:  # noqa: BLE001
        loaded.note_skip(GROUP_EDIT_FORMATS, name, _error(exc))
        return
    if not callable(applier):
        loaded.note_skip(GROUP_EDIT_FORMATS, name, "entry point is not a callable applier")
        return
    loaded.edit_formats[name] = applier


__all__ = [
    "GROUP_COMMANDS",
    "GROUP_EDIT_FORMATS",
    "GROUP_PROBES",
    "GROUP_PROVIDERS",
    "GROUP_TOOLS",
    "RESERVED_PROBE_IDS",
    "RESERVED_PROVIDER_TYPES",
    "LoadedPlugins",
    "SkippedPlugin",
    "load_plugins",
]
