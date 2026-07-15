"""Policy engine: maps (mode, tool risk) to a gate decision.

This table is the safety contract of the whole system (docs/SAFETY.md #3).
The turn engine MUST route every tool call through `decide()` before
execution — there is no other path to a tool. Changing this table is a
CONTRACTS.md change.

Note NET is never auto-allowed: network egress always asks, even in AUTO.
Command-level policy (deny-lists, classifiers — IC-402) layers on top of
this table; it can tighten a decision, never loosen it.
"""

from __future__ import annotations

from enum import StrEnum

from ironcore.safety.modes import Mode
from ironcore.safety.risk import ToolRisk


class Decision(StrEnum):
    ALLOW = "allow"  # execute without asking
    ASK = "ask"  # surface an approval prompt; denial cancels the call
    DENY = "deny"  # refuse outright; the model is told why


POLICY: dict[Mode, dict[ToolRisk, Decision]] = {
    Mode.PLAN: {
        ToolRisk.READ: Decision.ALLOW,
        ToolRisk.WRITE: Decision.DENY,
        ToolRisk.EXEC: Decision.DENY,
        ToolRisk.NET: Decision.DENY,
    },
    Mode.MANUAL: {
        ToolRisk.READ: Decision.ALLOW,
        ToolRisk.WRITE: Decision.ASK,
        ToolRisk.EXEC: Decision.ASK,
        ToolRisk.NET: Decision.ASK,
    },
    Mode.ACCEPT_EDITS: {
        ToolRisk.READ: Decision.ALLOW,
        ToolRisk.WRITE: Decision.ALLOW,
        ToolRisk.EXEC: Decision.ASK,
        ToolRisk.NET: Decision.ASK,
    },
    Mode.AUTO: {
        ToolRisk.READ: Decision.ALLOW,
        ToolRisk.WRITE: Decision.ALLOW,
        ToolRisk.EXEC: Decision.ALLOW,
        ToolRisk.NET: Decision.ASK,
    },
}


def decide(mode: Mode, risk: ToolRisk) -> Decision:
    """Gate decision for a tool call. Total over both enums by construction."""
    return POLICY[mode][risk]


#: Seed deny-list for the command policy engine (IC-402 extends this into a
#: real classifier). Matching is against the *resolved* command line, not the
#: raw model output. These are denied in EVERY mode, including AUTO.
DENYLIST_SEED: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf ~",
    "git push --force",
    "git push -f",
    "git reset --hard",
    "git clean -fdx",
    "mkfs",
    "format ",
    "shutdown",
    "reboot",
    "curl | sh",
    "curl | bash",
    "wget | sh",
    ":(){ :|:& };:",
    # IC-402 additions. Matching happens on the *normalized* command line
    # (casefolded, whitespace-collapsed, quotes stripped, shell wrappers
    # unwrapped — ironcore/safety/commands.py), so entries are lowercase with
    # single spaces. This tuple is NOT frozen (CONTRACTS §1) and may grow.
    "rm -fr /",
    "rm -fr ~",
    "--no-preserve-root",
    "wget | bash",
    "rd /s /q c:\\",
    "del /f /s /q c:\\",
    "format c:",
    "vssadmin delete shadows",
    "reg delete hklm",
)


#: Seed risky-pattern list for the command policy engine (IC-402). Regex
#: sources, compiled case-insensitively in ironcore/safety/commands.py and
#: matched against the normalized command line. A hit escalates a base ALLOW
#: to ASK (never loosens, never downgrades an ASK/DENY). NOT frozen
#: (CONTRACTS §1): entries may be added, base entries are never removed.
RISKY_PATTERN_SEED: tuple[str, ...] = (
    # source publishing / remote mutation
    r"\bgit\s+push\b",
    r"\b(?:npm|pnpm|yarn)\s+publish\b",
    r"\bpip3?\b[^|&;]*\bupload\b",
    r"\btwine\s+upload\b",
    r"\bcargo\s+publish\b",
    # recursive / forced deletes
    r"\brm\b(?=[^|&;]*\s-[a-z-]*r)(?=[^|&;]*\s-[a-z-]*f)",
    r"\brm\b[^|&;]*--recursive",
    r"\b(?:rd|rmdir)\b[^|&;]*/s\b",
    r"\bdel\b[^|&;]*/[fsq]\b",
    r"\bremove-item\b[^|&;]*-recurse",
    # privilege escalation
    r"\bsudo\b",
    r"\brunas\b",
    r"\bdoas\b",
    # pipe-to-shell (remote code straight into an interpreter)
    r"\b(?:curl|wget|iwr|invoke-webrequest)\b[^|]*\|\s*(?:\S*[\\/])?"
    r"(?:sh|bash|zsh|dash|pwsh|powershell)\b",
    r"\|\s*iex\b",
    # obfuscated payloads
    r"\b(?:powershell|pwsh)(?:\.exe)?\b[^|&;]*\s-e(?:nc[a-z]*)?\s",
    # disk / filesystem destruction
    r"\bmkfs\b",
    r"\bformat\s+[a-z]:",
    r"\bdiskpart\b",
    r"\bdd\b[^|&;]*\bof=/dev/",
)
