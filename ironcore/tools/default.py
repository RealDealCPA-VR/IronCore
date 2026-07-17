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
"""

from __future__ import annotations

from pathlib import Path

from ironcore.config.settings import Settings
from ironcore.tools.base import ToolRegistry
from ironcore.tools.fetch import FetchUrlTool
from ironcore.tools.fs_read import GlobTool, GrepTool, ListDirTool, ReadFileTool
from ironcore.tools.fs_write import EditFileTool, WriteFileTool
from ironcore.tools.image import ReadImageTool
from ironcore.tools.shell import ShellTool


def build_default_registry(settings: Settings, workspace: Path) -> ToolRegistry:
    """Assemble the default ToolRegistry for one session."""
    registry = ToolRegistry()
    for tool in (
        ReadFileTool(workspace),
        ListDirTool(workspace),
        GlobTool(workspace),
        GrepTool(workspace),
        WriteFileTool(workspace),
        EditFileTool(workspace),
        ShellTool(workspace),
        # read_image is registered UNCONDITIONALLY (the lineup is not frozen):
        # on a text-only model its vision_check degrade returns an honest error
        # instead of leaving the model to hallucinate what a file "shows".
        ReadImageTool(workspace),
    ):
        registry.register(tool)
    if settings.safety.network_tools:
        registry.register(FetchUrlTool())
    return registry
