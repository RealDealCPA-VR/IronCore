"""use_skill: the model-facing, lazy-body half of the SKILL.md standard (PKG-4).

The skills catalog in the system prompt lists available skills by NAME and a
one-line description (``ironcore/skills.py`` builds it, budget-fitted into the
SYSTEM share). When the model decides a task is covered by one, it calls
``use_skill(name=...)`` to pull that skill's full instructions — the lazy body
load the standard specifies. The body comes back as ``ToolResult.output`` and
rides the existing tool loop / transcript / audit like any other tool result.

RULES
-----
- READ risk: loading a skill only reads files under the user's home / workspace,
  side-effect-free (CONTRACTS §3). It passes ``decide(mode, risk)`` like every
  tool; reads are always allowed. A skill's *scripts* are a different matter —
  the model runs them via ``run_shell`` / ``run_command``, where the EXEC gate,
  deny-list and jail apply unchanged. Loading a body grants no execution.
- Project skills stay gated: a skill discovered under the WORKSPACE arrived by
  ``git clone``, so ``use_skill`` refuses to load it until the user has approved
  it once (``/skill run <name>``) — the same per-workspace confirmation the
  ``/skill`` command records (``skills.is_confirmed``). User-home skills are
  trusted (like ``~/.ironcore/IRONCORE.md``) and load without a prompt.
- Never raises for the model: an unknown name, an unapproved project skill, or a
  missing argument all return ``ToolResult(ok=False)`` with the reason mirrored
  into ``output`` (the engine feeds only ``output`` back to the model).
- Re-discovers on every call so a skill added or approved mid-session is found;
  ``user_home`` is injectable for hermetic tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ironcore.safety.risk import ToolRisk
from ironcore.skills import MAX_BODY_CHARS, discover_skills, is_confirmed
from ironcore.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from ironcore.config.settings import Settings


class UseSkillTool(Tool):
    """Load a named skill's full instructions (the standard's lazy-body step)."""

    name = "use_skill"
    risk = ToolRisk.READ
    description = (
        "Load the full step-by-step instructions for a named skill (a reusable "
        "procedure). The skills catalog in the system prompt lists available skills "
        "by name; call this to read one BEFORE doing a task it covers. "
        "Example: use_skill(name='release-checklist')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name exactly as shown in the skills catalog.",
            }
        },
        "required": ["name"],
    }

    def __init__(
        self, workspace: Path, settings: Settings, *, user_home: Path | None = None
    ) -> None:
        self._workspace = Path(workspace)
        self._settings = settings
        self._user_home = user_home

    async def run(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name")
        if not isinstance(name, str) or not name.strip():
            return ToolResult(
                ok=False,
                output="use_skill needs a 'name' argument naming the skill to load.",
                error="missing name",
            )
        loaded = discover_skills(self._workspace, self._settings, user_home=self._user_home)
        skill = loaded.find(name)
        if skill is None:
            available = ", ".join(s.name for s in loaded.trusted(self._workspace)) or "(none)"
            return ToolResult(
                ok=False,
                output=f"No skill named {name!r}. Available skills: {available}.",
                error="unknown skill",
            )
        if skill.source == "project" and not is_confirmed(self._workspace, skill.name):
            return ToolResult(
                ok=False,
                output=(
                    f"Skill {skill.name!r} is a project skill from this repository and needs "
                    f"one-time user approval before it can be loaded. Ask the user to run "
                    f"/skill {skill.name} to review and approve it; then call use_skill again."
                ),
                error="project skill not approved",
            )
        body = skill.body or "(this skill has an empty body)"
        if len(body) > MAX_BODY_CHARS:
            dropped = len(body) - MAX_BODY_CHARS
            body = body[:MAX_BODY_CHARS] + f"\n... [skill body truncated: {dropped} more chars]"
        return ToolResult(ok=True, output=f"# Skill: {skill.name}\n\n{body}")
