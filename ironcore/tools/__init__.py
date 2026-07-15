"""Tool suite: the hands of the agent.

Concrete tools land in phases 3 (fs/shell) and beyond — see TODO.md
IC-301..IC-304. The base contract and registry live here and are frozen
in docs/CONTRACTS.md.
"""

from ironcore.tools.base import Tool, ToolRegistry, ToolResult

__all__ = ["Tool", "ToolRegistry", "ToolResult"]
