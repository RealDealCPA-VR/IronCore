"""Command policy engine: layered tightening on top of the mode gate (IC-402).

Rules of this module (SAFETY §2 T1/T8, SPEC §7.4, CONTRACTS §1):

- **Tighten only.** `classify_command()` starts from `decide(mode, ToolRisk.EXEC)`
  and may move a decision allow → ask → deny, never the other direction. There
  is no code path that returns a decision looser than the mode gate's.
- **Deny-list wins everywhere.** A deny-list hit is DENY in every mode,
  including AUTO. No mode skips the deny-list (SAFETY §7).
- **Risky patterns escalate autonomy, not oversight.** A risky-pattern hit
  turns a base ALLOW into ASK; a base ASK or DENY is left untouched.
- **Normalization defeats trivial bypasses.** Matching runs against a
  casefolded, whitespace-collapsed, quote-stripped command line with common
  shell wrappers (`cmd /c`, `powershell -Command`, `sh -c`, ...) unwrapped —
  recursively, so nested wrappers cannot hide a payload. Every intermediate
  unwrap stage is matched, so a wrapper prefix itself (`sudo sh -c ...`) is
  still seen. This is best-effort defense-in-depth; the mode gate remains the
  primary control.
- **Ceiling rule (SAFETY T8).** `CommandPolicy` always contains the seed rules
  from `ironcore.safety.policy`; extra rules (e.g. from config) are merged
  additively via `with_extra_rules()`. No API removes a base rule, so a
  project config can add restrictions but can never loosen the user's ceiling.
- **Fail loud at build time.** Malformed extra rules (empty strings, invalid
  regexes) raise ValueError when the policy is constructed — the config layer
  translates that into its own error type; a broken rule never silently
  becomes a no-op.
- Stdlib only (dependency rule, docs/ARCHITECTURE.md §4).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ironcore.safety.modes import Mode
from ironcore.safety.policy import DENYLIST_SEED, RISKY_PATTERN_SEED, Decision, decide
from ironcore.safety.risk import ToolRisk

_WS_RUN = re.compile(r"\s+")

# Shell wrapper prefixes, matched against the scrubbed (casefolded, collapsed,
# quote-stripped) command line. Group "inner" is the wrapped payload.
_WRAPPERS: tuple[re.Pattern[str], ...] = (
    # cmd /c ... | cmd.exe /s /c ... | c:\windows\system32\cmd.exe /c ...
    re.compile(r"^(?:\S*[\\/])?cmd(?:\.exe)?\s+(?:/[a-z:.=-]+\s+)*/[ck]\s+(?P<inner>.+)$"),
    # powershell -Command ... | pwsh -NoProfile -c ... (skips leading flags)
    re.compile(
        r"^(?:\S*[\\/])?(?:powershell|pwsh)(?:\.exe)?(?:\s+\S+)*?"
        r"\s+-c(?:ommand)?\s+(?P<inner>.+)$"
    ),
    # sh -c ... | bash -lc is NOT unwrapped, but bash -l -c ... is;
    # /bin/sh -c ... | /usr/bin/env bash -c ...
    re.compile(
        r"^(?:\S*[\\/])?(?:env\s+(?:\S*[\\/])?)?(?:sh|bash|zsh|dash|ksh)(?:\.exe)?"
        r"\s+(?:-\S+\s+)*-c\s+(?P<inner>.+)$"
    ),
)


def _scrub(text: str) -> str:
    """Casefold, collapse whitespace runs, strip quote characters."""
    text = _WS_RUN.sub(" ", text.casefold()).strip()
    return text.replace('"', "").replace("'", "").replace("`", "")


def _unwrap_once(cmd: str) -> str:
    """Strip one shell-wrapper prefix; return the input unchanged on no match."""
    for pattern in _WRAPPERS:
        match = pattern.match(cmd)
        if match:
            return match.group("inner").strip()
    return cmd


def _stages(cmd: str) -> tuple[str, ...]:
    """All normalization stages: scrubbed input, then each unwrap level.

    Matching every stage (not just the innermost) means unwrapping can only
    widen what the classifier sees — a wrapper prefix can never hide a match
    that was visible before unwrapping.
    """
    current = _scrub(cmd)
    stages = [current]
    while True:
        inner = _unwrap_once(current)
        if inner == current or not inner:
            break
        stages.append(inner)
        current = inner  # each unwrap strictly shortens the string: terminates
    return tuple(stages)


def normalize_command(cmd: str) -> str:
    """Normalized form of a command line for policy matching.

    Casefolds, collapses whitespace, strips quote characters, and unwraps
    common shell prefixes (``cmd /c``, ``cmd.exe /c``, ``powershell
    -Command``, ``pwsh -c``, ``sh -c``, ``bash -c``, ``/bin/sh -c``)
    recursively — nested wrappers are fully unwrapped, not just one level.
    Returns the innermost command.
    """
    return _stages(cmd)[-1]


def _prepare_deny(entries: Iterable[str], *, source: str) -> tuple[str, ...]:
    """Scrub deny entries the same way commands are scrubbed.

    Internal whitespace runs collapse but edges are preserved: the seed entry
    ``"format "`` needs its trailing space to avoid matching ``--format=...``.
    """
    prepared: list[str] = []
    for entry in entries:
        cleaned = _WS_RUN.sub(" ", entry.casefold())
        cleaned = cleaned.replace('"', "").replace("'", "").replace("`", "")
        if not cleaned.strip():
            raise ValueError(f"{source}: empty deny rule would match every command")
        prepared.append(cleaned)
    return tuple(prepared)


def _prepare_risky(entries: Iterable[str], *, source: str) -> tuple[re.Pattern[str], ...]:
    compiled: list[re.Pattern[str]] = []
    for entry in entries:
        if not entry.strip():
            raise ValueError(f"{source}: empty risky rule would match every command")
        try:
            compiled.append(re.compile(entry, re.IGNORECASE))
        except re.error as exc:
            raise ValueError(f"{source}: invalid risky-pattern regex {entry!r}: {exc}") from exc
    return tuple(compiled)


class CommandPolicy:
    """Effective deny-list + risky patterns: the seeds plus additive extras.

    The seeds (`DENYLIST_SEED`, `RISKY_PATTERN_SEED`) are baked into every
    instance by the constructor — a caller can add rules but cannot construct
    a policy without the base, and no method removes a rule (SAFETY T8).
    """

    def __init__(self, extra_deny: Iterable[str] = (), extra_risky: Iterable[str] = ()) -> None:
        self._extra_deny = tuple(dict.fromkeys(extra_deny))
        self._extra_risky = tuple(dict.fromkeys(extra_risky))
        # Base always present; extras validated fail-loud at build time.
        self._deny = tuple(
            dict.fromkeys(
                _prepare_deny(DENYLIST_SEED, source="DENYLIST_SEED")
                + _prepare_deny(self._extra_deny, source="extra deny rules")
            )
        )
        self._risky = _prepare_risky(
            RISKY_PATTERN_SEED, source="RISKY_PATTERN_SEED"
        ) + _prepare_risky(self._extra_risky, source="extra risky rules")

    @property
    def deny_rules(self) -> tuple[str, ...]:
        """Effective deny-list (scrubbed), seed rules first."""
        return self._deny

    @property
    def risky_rules(self) -> tuple[str, ...]:
        """Effective risky-pattern sources, seed rules first."""
        return tuple(pattern.pattern for pattern in self._risky)

    def with_extra_rules(
        self, deny: Iterable[str] = (), risky: Iterable[str] = ()
    ) -> CommandPolicy:
        """Additive merge: a new policy with these rules added.

        Ceiling rule: the result keeps every rule this policy has (seed and
        prior extras). There is no subtractive counterpart.
        """
        return CommandPolicy(
            extra_deny=self._extra_deny + tuple(deny),
            extra_risky=self._extra_risky + tuple(risky),
        )

    def classify(self, cmd: str, mode: Mode) -> Decision:
        """Tighten the mode gate's EXEC decision for a concrete command line.

        Deny-list hit → DENY in every mode. Risky-pattern hit → a base ALLOW
        becomes ASK; ASK/DENY are left alone. Never loosens.
        """
        base = decide(mode, ToolRisk.EXEC)
        stages = _stages(cmd)
        for stage in stages:
            if any(entry in stage for entry in self._deny):
                return Decision.DENY
        if base is Decision.ALLOW:
            for stage in stages:
                if any(pattern.search(stage) for pattern in self._risky):
                    return Decision.ASK
        return base


#: Module default: seeds only. The engine uses this unless config adds rules.
DEFAULT_POLICY = CommandPolicy()


def classify_command(cmd: str, mode: Mode) -> Decision:
    """Classify a command under the seed-only default policy (see CommandPolicy)."""
    return DEFAULT_POLICY.classify(cmd, mode)
