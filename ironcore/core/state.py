"""Session state: the harness-owned record the model never has to remember.

Rules of this module (docs/ARCHITECTURE.md #5):
* Owns mode / goal / working set / plan cursor / turn count / budgets spent.
  Anything the model needs from here is re-presented at COMPOSE time.
* Persists as JSON at `<workspace>/.ironcore/state.json` (see `state_path`).
  Saves are atomic: write to a temp file in the same directory, then
  `os.replace` — atomic on both Windows and POSIX.
* Loading NEVER raises on a missing, unreadable, or corrupt file. Callers
  get `(fresh SessionState, warning | None)`; a session must always boot.
* `Mode` serializes as its string value and round-trips through the enum.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ironcore.safety.modes import Mode

#: On-disk schema marker, written into every state file. Purely additive for
#: now; shape drift is caught by `from_dict` validation, not this number.
STATE_VERSION = 1

STATE_FILENAME = "state.json"


def state_path(workspace: Path) -> Path:
    """Canonical location of the session state file for a workspace."""
    return workspace / ".ironcore" / STATE_FILENAME


@dataclass
class SessionState:
    """One session's harness-owned state. All fields have boot defaults so a
    fresh instance is always a valid "nothing has happened yet" session."""

    mode: Mode = Mode.MANUAL
    goal: str | None = None
    #: Workspace-relative path strings, most-recently-used first.
    working_set: list[str] = field(default_factory=list)
    plan_steps: list[str] = field(default_factory=list)
    plan_cursor: int = 0
    #: Step index -> evidence the step completed (command output, test tail).
    plan_evidence: dict[int, str] = field(default_factory=dict)
    turn_count: int = 0
    #: e.g. {"tokens": 1234, "provider_calls": 5, "wall_clock_s": 12.5}
    budgets_spent: dict[str, float | int] = field(default_factory=dict)

    def touch(self, rel_path: str) -> None:
        """Mark a working-set file as just used: move (or insert) it at the
        MRU front without duplicating it."""
        if rel_path in self.working_set:
            self.working_set.remove(rel_path)
        self.working_set.insert(0, rel_path)

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "mode": self.mode.value,
            "goal": self.goal,
            "working_set": list(self.working_set),
            "plan_steps": list(self.plan_steps),
            "plan_cursor": self.plan_cursor,
            # JSON object keys must be strings; from_dict converts them back.
            "plan_evidence": {str(k): v for k, v in self.plan_evidence.items()},
            "turn_count": self.turn_count,
            "budgets_spent": dict(self.budgets_spent),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        """Strict enough to reject garbage (raises ValueError/TypeError/KeyError,
        which `load` turns into fresh-state-plus-warning), lenient enough that
        missing keys fall back to defaults (additive schema evolution)."""
        if not isinstance(data, dict):
            raise TypeError(f"state root must be an object, got {type(data).__name__}")
        state = cls()
        state.mode = Mode(data.get("mode", Mode.MANUAL.value))  # ValueError on unknown
        state.goal = _checked(data.get("goal"), str, "goal", nullable=True)
        state.working_set = [
            _checked(p, str, "working_set item") for p in data.get("working_set", [])
        ]
        state.plan_steps = [_checked(s, str, "plan_steps item") for s in data.get("plan_steps", [])]
        state.plan_cursor = _checked(data.get("plan_cursor", 0), int, "plan_cursor")
        state.plan_evidence = {
            int(k): _checked(v, str, "plan_evidence value")
            for k, v in _checked(data.get("plan_evidence", {}), dict, "plan_evidence").items()
        }
        state.turn_count = _checked(data.get("turn_count", 0), int, "turn_count")
        state.budgets_spent = {
            _checked(k, str, "budgets_spent key"): _checked(v, (int, float), "budgets_spent value")
            for k, v in _checked(data.get("budgets_spent", {}), dict, "budgets_spent").items()
        }
        return state

    # -- persistence ---------------------------------------------------------

    def save(self, path: Path) -> None:
        """Atomically write to `path`, creating parent directories as needed.

        The temp file lives next to the target (same volume, so `os.replace`
        stays atomic) under a fixed name; a leftover from an interrupted save
        is simply overwritten by the next one.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        payload = json.dumps(self.to_dict(), indent=2) + "\n"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> tuple[SessionState, str | None]:
        """Load state from `path`. Never raises.

        Returns `(state, warning)`: a missing file is the normal first boot
        (fresh state, no warning); an unreadable or corrupt file yields a
        fresh state plus a warning string for the front end to surface.
        """
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(), None
        except OSError as exc:
            return cls(), f"state file unreadable, starting fresh: {path} ({exc})"
        try:
            return cls.from_dict(json.loads(raw)), None
        except (ValueError, TypeError, KeyError) as exc:
            return cls(), f"state file corrupt, starting fresh: {path} ({exc})"


def _checked(
    value: Any, types: type | tuple[type, ...], label: str, *, nullable: bool = False
) -> Any:
    """Return `value` if it is an instance of `types` (or None when nullable);
    raise TypeError otherwise. bool is rejected where int is expected — JSON
    `true` must not sneak into counters."""
    if value is None and nullable:
        return value
    if isinstance(value, bool) and bool not in (types if isinstance(types, tuple) else (types,)):
        raise TypeError(f"{label} must not be a boolean")
    if not isinstance(value, types):
        raise TypeError(f"{label} has wrong type {type(value).__name__}")
    return value
