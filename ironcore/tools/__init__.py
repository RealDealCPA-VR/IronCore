"""Tool suite: the hands of the agent.

The base contract and registry are frozen in docs/CONTRACTS.md §3.
Concrete tools: fs_read (READ), fs_write (WRITE), shell (EXEC),
fetch (NET — registered only when safety.network_tools is true).
``build_default_registry`` assembles the standard lineup per SPEC §6.1.
"""

from ironcore.tools.base import Tool, ToolRegistry, ToolResult
from ironcore.tools.default import build_default_registry
from ironcore.tools.fetch import FetchUrlTool
from ironcore.tools.fs_read import GlobTool, GrepTool, ListDirTool, ReadFileTool
from ironcore.tools.fs_write import EditFileTool, WriteFileTool
from ironcore.tools.shell import ShellTool

__all__ = [
    "EditFileTool",
    "FetchUrlTool",
    "GlobTool",
    "GrepTool",
    "ListDirTool",
    "ReadFileTool",
    "ShellTool",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "WriteFileTool",
    "build_default_registry",
]
