"""Operating modes, cycled in the TUI with Shift+Tab.

The four modes trade autonomy for oversight. MANUAL is the default.
The cycle order is chosen so one Shift+Tab from the default grants more
autonomy (the common case), and PLAN sits at the end of the loop as the
deliberate "hands off the keyboard" choice.
"""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    PLAN = "plan"  # read-only: explore, reason, propose; no writes, no exec
    MANUAL = "manual"  # every write/exec/net action is individually approved
    ACCEPT_EDITS = "accept-edits"  # file edits auto-apply (jailed); commands still ask
    AUTO = "auto"  # full auto inside the sandbox; network still asks


#: Shift+Tab cycle order. MANUAL is the boot default.
CYCLE: list[Mode] = [Mode.MANUAL, Mode.ACCEPT_EDITS, Mode.AUTO, Mode.PLAN]

DESCRIPTIONS: dict[Mode, str] = {
    Mode.PLAN: "Read-only. Explore and propose; nothing is changed.",
    Mode.MANUAL: "Approve every file edit, command, and network call.",
    Mode.ACCEPT_EDITS: "File edits apply automatically; commands still ask.",
    Mode.AUTO: "Full auto inside the workspace sandbox; network still asks.",
}


def next_mode(current: Mode) -> Mode:
    """Return the next mode in the Shift+Tab cycle."""
    idx = CYCLE.index(current)
    return CYCLE[(idx + 1) % len(CYCLE)]
