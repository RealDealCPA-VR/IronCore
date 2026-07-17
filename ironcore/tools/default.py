"""Default toolset assembly (SPEC §6.1).

``build_default_registry`` is the ONE place the standard tool lineup is
defined; the engine (IC-502) calls it at boot and the TUI lists whatever
it produced. Rules:

- fs + shell tools are ALWAYS registered; ``fetch_url`` is registered
  only when ``settings.safety.network_tools`` is true. An off NET tool
  is not merely gated — it is never registered, so the model never
  sees it in the tool specs.
- Tools receive the workspace their constructors require; nothing here
  gates or prints (CONTRACTS §3) — the safety policy runs in the engine.
- Plugin tools (MS-5) register AFTER the builtins with duplicate-skip:
  a plugin can never shadow a builtin name (``read_file`` … ``read_image``),
  and its edit formats reach ``EditFileTool`` as ``extra_formats``. The
  ``plugins=`` value is an already-loaded ``LoadedPlugins`` — this module
  never imports ``ironcore.plugins`` at runtime (layering: tools/ imports
  safety + config only).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ironcore.config.settings import Settings
from ironcore.tools.base import ToolRegistry
from ironcore.tools.fetch import FetchUrlTool
from ironcore.tools.fs_read import GlobTool, GrepTool, ListDirTool, ReadFileTool
from ironcore.tools.fs_write import EditFileTool, WriteFileTool
from ironcore.tools.image import ReadImageTool
from ironcore.tools.shell import ShellTool

if TYPE_CHECKING:
    from pathlib import Path

    from ironcore.plugins import LoadedPlugins


def build_default_registry(
    settings: Settings, workspace: Path, *, plugins: LoadedPlugins | None = None
) -> ToolRegistry:
    """Assemble the default ToolRegistry for one session."""
    registry = ToolRegistry()
    extra_formats = plugins.edit_formats if plugins is not None else None
    for tool in (
        ReadFileTool(workspace),
        ListDirTool(workspace),
        GlobTool(workspace),
        GrepTool(workspace),
        WriteFileTool(workspace),
        EditFileTool(workspace, extra_formats=extra_formats),
        ShellTool(workspace),
        # read_image is registered UNCONDITIONALLY (the lineup is not frozen):
        # on a text-only model its vision_check degrade returns an honest error
        # instead of leaving the model to hallucinate what a file "shows".
        ReadImageTool(workspace),
    ):
        registry.register(tool)
    if settings.safety.network_tools:
        registry.register(FetchUrlTool())
    if plugins is not None:
        # Builtins win: a duplicate plugin name is skipped and recorded, so
        # doctor/boot notes can say why a plugin tool never appeared. The
        # loader already refused NET-risk tools when network_tools is false.
        for tool in plugins.tools:
            if registry.get(tool.name) is not None:
                plugins.note_skip(
                    "ironcore.tools",
                    tool.name,
                    f"duplicate of registered tool {tool.name!r}; built-ins win",
                )
                continue
            registry.register(tool)
    return registry
