"""Skills: the SKILL.md open standard, adapted to IronCore (PKG-4).

A *skill* is a folder holding a ``SKILL.md`` file — YAML frontmatter naming the
skill (``name`` + ``description``) over a Markdown body of standing
instructions. It is the same on-disk shape Claude Code, Codex, grok-build and
~20 other tools already read, so a skill authored for any of them works here
unchanged. This module is IronCore's discovery + surfacing layer, modeled on
``ironcore/plugins.py`` and sharing its fail-safe discipline: a malformed
``SKILL.md`` is SKIPPED with a recorded reason, never a crash.

DISCOVERY (``discover_skills``)
- USER-GLOBAL: ``<home>/.ironcore/skills/<name>/SKILL.md`` — the user's own
  machine-wide skills, TRUSTED like ``~/.ironcore/IRONCORE.md``.
- PROJECT: ``<workspace>/.ironcore/skills/<name>/SKILL.md`` — arrives with a
  ``git clone``, so its FIRST USE is gated (see the SAFETY note below).
- COMPAT (``[skills] compat_dirs = true``): additionally reads ``.claude`` /
  ``.codex`` / ``.grok`` ``/skills`` at BOTH levels, so ecosystem skills need no
  copy. Off by default.
User roots are scanned before project roots; a name is unique (first wins), so
a cloned project skill can never shadow one of the user's own.

SURFACING (``load_skills_catalog``)
- A compact catalog (``- name: one-liner`` per skill) is injected beside project
  memory in ``core/composer._build_system``, charged to the SYSTEM share against
  the MEASURED ``honest_context``. The envelope-native twist: on a tiny-context
  model the catalog degrades to top-N (or nothing) rather than silently eating
  the window. Only TRUSTED skills reach that model-facing catalog — user skills,
  and project skills the user has confirmed — so an UNCONFIRMED cloned skill's
  description never lands in the trusted, un-scanned system prompt.

INVOCATION (two paths, both lazy-body per the standard)
- ``/skill <name>`` (``commands/skillcmd.py``) injects the full body into the
  next turn.
- ``use_skill`` (``tools/skill.py``), a READ-risk tool, returns the body as
  ``ToolResult.output`` — riding the existing tool loop / transcript / audit.

SAFETY (T8 spirit — mirrors ``commands/workflowcmd.py``'s first-run gate)
- USER skills are trusted (the user authored them on their own machine).
- PROJECT skills arrive by clone, so their FIRST USE in a workspace requires a
  one-time confirmation (``/skill run <name>``). Until then they are absent from
  the model catalog and ``use_skill`` refuses to load them. The shared
  confirmation registry (``is_confirmed`` / ``mark_confirmed``) is consulted by
  BOTH the command and the tool.
- A skill CANNOT smuggle execution past the kernel: any script it references is
  run by the model through ``run_shell`` / ``run_command``, so the EXEC gate,
  deny-list and write-jail apply unchanged. Skill bodies are DISPLAY text — this
  module never parses a ``verify:`` directive or any other control channel out
  of one (that path stays sourced from the project ``IRONCORE.md`` alone, per
  ``core/verify.py`` and the ``core/composer`` SECURITY note).

This module imports only stdlib + ``config`` + ``yaml`` (already a dependency).
Layered packages take an already-loaded result; nothing here imports ``tui/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from ironcore.config.settings import Settings

#: Per-skill description cap (matches ``tools/mcp.py`` — the catalog rides every
#: prompt, so a paragraph-long description would squeeze a small context).
MAX_DESCRIPTION_CHARS = 300

#: Cap on the body ``use_skill`` returns (``tools/mcp.py`` output scale): a
#: pathological SKILL.md cannot dump megabytes into the model's context.
MAX_BODY_CHARS = 50_000

#: The one filename a skill folder must contain (the open standard).
SKILL_FILENAME = "SKILL.md"

#: Ecosystem-compat parents scanned under ``[skills] compat_dirs = true``. Each
#: gets a ``/skills`` child — the exact layout the named tool already uses.
COMPAT_PARENTS = (".claude", ".codex", ".grok")


def _norm(text: str) -> str:
    """Fold a name to its match key: collapse whitespace, casefold."""
    return " ".join(text.split()).casefold()


@dataclass(frozen=True)
class Skill:
    """One discovered skill: its metadata plus the Markdown body (instructions).

    ``source`` is ``"user"`` (trusted) or ``"project"`` (clone-borne, first-use
    gated). ``dir_name`` is the containing folder — a fallback match key when a
    skill omits ``name``.
    """

    name: str
    description: str
    body: str
    path: Path
    source: str
    dir_name: str

    def matches(self, query: str) -> bool:
        key = _norm(query)
        return key in (_norm(self.name), _norm(self.dir_name))

    def catalog_line(self) -> str:
        """One compact catalog row for the model-facing SYSTEM share."""
        return f"- {self.name}: {self.description or '(no description)'}"


@dataclass(frozen=True)
class SkippedSkill:
    """One ``SKILL.md`` that did not load, and why (surfaced by ``/skill``)."""

    path: str
    reason: str


@dataclass
class LoadedSkills:
    """Everything discovery produced: the usable skills and the skipped files."""

    skills: list[Skill] = field(default_factory=list)
    skipped: list[SkippedSkill] = field(default_factory=list)

    def find(self, query: str) -> Skill | None:
        """First skill whose name or directory matches ``query`` (normalized)."""
        for skill in self.skills:
            if skill.matches(query):
                return skill
        return None

    def trusted(self, workspace: Path | str | None) -> list[Skill]:
        """User skills, plus project skills confirmed for ``workspace``."""
        return [
            s
            for s in self.skills
            if s.source != "project" or is_confirmed(workspace, s.name)
        ]


# --------------------------------------------------------------------------- #
# per-workspace first-use confirmation (shared by /skill and use_skill; T8)
# --------------------------------------------------------------------------- #

#: workspace-key -> normalized names confirmed (approved) in that workspace.
#: Durable across the ephemeral ``CommandContext`` the TUI rebuilds per dispatch
#: (mirrors ``commands/workflowcmd.py``'s ``_CONFIRMED``).
_CONFIRMED: dict[str, set[str]] = {}


def _ws_key(workspace: Path | str | None) -> str:
    if workspace is None:
        return "<no-workspace>"
    try:
        return str(Path(workspace).resolve())
    except OSError:
        return str(workspace)


def is_confirmed(workspace: Path | str | None, name: str) -> bool:
    """Whether project skill ``name`` has been approved for ``workspace``."""
    return _norm(name) in _CONFIRMED.get(_ws_key(workspace), set())


def mark_confirmed(workspace: Path | str | None, name: str) -> None:
    """Record that the user approved project skill ``name`` for ``workspace``."""
    _CONFIRMED.setdefault(_ws_key(workspace), set()).add(_norm(name))


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def discover_skills(
    workspace: Path | str,
    settings: Settings,
    *,
    user_home: Path | None = None,
) -> LoadedSkills:
    """Discover every skill visible to a session. Never raises.

    ``settings.skills.enabled = False`` short-circuits to an empty result.
    ``user_home`` defaults to ``Path.home()`` and is injectable for tests.
    """
    loaded = LoadedSkills()
    skills_cfg = getattr(settings, "skills", None)
    if skills_cfg is not None and not skills_cfg.enabled:
        return loaded
    compat = bool(getattr(skills_cfg, "compat_dirs", False))
    home = user_home if user_home is not None else Path.home()
    seen: set[str] = set()
    for root, source in _scan_roots(Path(workspace), home, compat=compat):
        for skill_md in _skill_files(root):
            skill, reason = _parse_skill(skill_md, source)
            if skill is None:
                loaded.skipped.append(SkippedSkill(str(skill_md), reason or "unparseable"))
                continue
            key = _norm(skill.name)
            if key in seen:
                loaded.skipped.append(
                    SkippedSkill(str(skill_md), f"duplicate skill name {skill.name!r}; first wins")
                )
                continue
            seen.add(key)
            loaded.skills.append(skill)
    return loaded


def _scan_roots(workspace: Path, home: Path, *, compat: bool) -> list[tuple[Path, str]]:
    """Ordered ``(dir, source)`` roots: user first (trusted), project second.

    Deduped by resolved path so a workspace that happens to be the home dir is
    scanned once (and as the trusted ``"user"`` source)."""
    candidates: list[tuple[Path, str]] = [(home / ".ironcore" / "skills", "user")]
    if compat:
        candidates += [(home / parent / "skills", "user") for parent in COMPAT_PARENTS]
    candidates.append((workspace / ".ironcore" / "skills", "project"))
    if compat:
        candidates += [(workspace / parent / "skills", "project") for parent in COMPAT_PARENTS]

    roots: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path, source in candidates:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append((path, source))
    return roots


def _skill_files(root: Path) -> list[Path]:
    """Every ``<root>/<name>/SKILL.md``, sorted for determinism. Never raises."""
    try:
        if not root.is_dir():
            return []
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    files: list[Path] = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            candidate = entry / SKILL_FILENAME
            if candidate.is_file():
                files.append(candidate)
        except OSError:
            continue
    return files


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Split ``---``-delimited YAML frontmatter from the Markdown body.

    Returns ``(frontmatter, body)`` or ``None`` when the file does not open with
    a ``---`` line or the fence is never closed. Line-based so CRLF and LF files
    parse identically; the body is normalized to ``\\n`` line endings.
    """
    if text and ord(text[0]) == 0xFEFF:
        text = text[1:]  # a leading Windows editor BOM is not a syntax error
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    return None  # unterminated frontmatter


def _first_line(exc: Exception) -> str:
    parts = str(exc).splitlines()
    return parts[0] if parts else exc.__class__.__name__


def _parse_skill(path: Path, source: str) -> tuple[Skill | None, str | None]:
    """Parse one ``SKILL.md`` into a ``Skill``. ``(None, reason)`` on any defect —
    the caller records it and moves on (fail-safe, like ``plugins.py``)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"unreadable: {exc}"
    split = _split_frontmatter(raw)
    if split is None:
        return None, "missing or unterminated YAML frontmatter (--- delimited)"
    fm_text, body = split
    try:
        meta = yaml.safe_load(fm_text) if fm_text.strip() else {}
    except yaml.YAMLError as exc:
        return None, f"invalid YAML frontmatter: {_first_line(exc)}"
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        return None, "YAML frontmatter is not a mapping"

    dir_name = path.parent.name
    raw_name = meta.get("name")
    name = " ".join(str(raw_name).split()) if raw_name is not None else ""
    if not name:
        name = dir_name  # lenient: fall back to the folder name
    if not name:
        return None, "skill has no name and no directory name"

    description = " ".join(str(meta.get("description") or "").split())
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[: MAX_DESCRIPTION_CHARS - 3] + "..."

    skill = Skill(
        name=name,
        description=description,
        body=body.strip(),
        path=path,
        source=source,
        dir_name=dir_name,
    )
    return skill, None


# --------------------------------------------------------------------------- #
# surfacing (the model-facing catalog)
# --------------------------------------------------------------------------- #


def load_skills_catalog(
    workspace: Path | str,
    settings: Settings,
    *,
    user_home: Path | None = None,
) -> list[str]:
    """The catalog lines for the SYSTEM share: TRUSTED skills only.

    A convenience over ``discover_skills`` + filtering, called by the engine each
    turn (like ``load_project_memory``) so mid-session skill edits and freshly
    confirmed project skills land. Composer budget-fits and honestly degrades the
    lines it is handed; this function only decides WHICH skills are eligible.
    """
    loaded = discover_skills(workspace, settings, user_home=user_home)
    return [skill.catalog_line() for skill in loaded.trusted(workspace)]


__all__ = [
    "COMPAT_PARENTS",
    "MAX_BODY_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "SKILL_FILENAME",
    "LoadedSkills",
    "Skill",
    "SkippedSkill",
    "discover_skills",
    "is_confirmed",
    "load_skills_catalog",
    "mark_confirmed",
]
