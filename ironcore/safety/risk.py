"""Risk taxonomy for tools.

Every tool declares exactly one ToolRisk class. The policy engine maps
(mode, risk) -> decision. A tool that cannot honestly pick one class must
be split into two tools (e.g. `git_status` READ vs `git_commit` WRITE).
"""

from __future__ import annotations

from enum import StrEnum


class ToolRisk(StrEnum):
    """What a tool can affect, worst case.

    READ  — inspects the workspace or session; cannot change anything.
    WRITE — creates or modifies files inside the workspace jail.
    EXEC  — runs a subprocess; may do anything the command policy allows.
    NET   — talks to the network (fetch, search, API calls).
    """

    READ = "read"
    WRITE = "write"
    EXEC = "exec"
    NET = "net"
