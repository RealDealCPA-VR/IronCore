"""/review (IC-806): review the working git diff for bugs via the verifier model.

``/review`` collects the workspace's working diff (``git diff HEAD``, falling
back to ``git diff`` before the first commit), sends it to the verifier-role
model under a bug-focused rubric, and reports the findings as
``<file>:<line> <severity> — <problem>`` lines (or "no findings"). It is ASYNC —
the git call and the model call both happen off the UI thread via ``schedule``;
the handler returns an ack immediately.

Degradations, all honest: not a git repo → say so; a clean tree → nothing to
review; the model returns unstructured text → surface it verbatim rather than
dropping it; the diff is huge → truncate before sending.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

from ironcore.commands._helpers import resolve_provider, resolve_workspace
from ironcore.commands.base import CommandContext, SlashCommand
from ironcore.providers.base import Message, SamplingPolicy
from ironcore.providers.openai_compat import ProviderError

#: Cap on diff chars sent to the model (context + latency guard).
_MAX_DIFF_CHARS = 30_000

_NO_FINDINGS = "NO FINDINGS"

_RUBRIC = (
    "You are a meticulous code reviewer. Review the unified diff below for BUGS "
    "ONLY: logic errors, off-by-one mistakes, unhandled errors or exceptions, "
    "resource leaks, injection/security issues, broken edge cases, and "
    "regressions. Ignore pure style. For each issue, output ONE line and nothing "
    "else:\n"
    "<file>:<line> <high|medium|low> — <the concrete problem>\n"
    f"If you find no bugs, output exactly: {_NO_FINDINGS}"
)

#: A finding line looks like ``path/to/file.py:123 high — ...`` (optional bullet).
_FINDING_RE = re.compile(r"^\S.*?:\d+\b")


def _cmd_review(ctx: CommandContext, args: str) -> str:
    workspace = resolve_workspace(ctx)
    if workspace is None:
        return "No workspace available to review."
    schedule = ctx.extra.get("schedule")
    if schedule is None:
        return "Review needs the scheduler (not available here)."
    provider = resolve_provider(ctx, role="verifier")
    if provider is None:
        return "Review needs a live model (no provider available)."
    schedule(_review_coro(workspace, provider))
    return "Reviewing the working diff for bugs…"


async def _review_coro(workspace: Path, provider) -> str:
    diff, note = await asyncio.to_thread(_working_diff, workspace)
    if diff is None:
        return note
    prompt = f"{note}\n\n```diff\n{diff}\n```"
    try:
        result = await provider.complete(
            [
                Message(role="system", content=_RUBRIC),
                Message(role="user", content=prompt),
            ],
            sampling=SamplingPolicy(temperature=0.0, top_p=1.0),
        )
    except ProviderError as exc:
        return f"Review failed: {exc}"
    return _format_findings((result.message.content or "").strip())


# -- git plumbing -------------------------------------------------------------


def _git(workspace: Path, *args: str) -> str | None:
    """Run git in ``workspace``; stdout on success, ``None`` on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _working_diff(workspace: Path) -> tuple[str | None, str]:
    """Return ``(diff_text|None, note)`` for the workspace's working diff."""
    inside = _git(workspace, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != "true":
        return None, "Not a git repository — /review needs git to compute the working diff."
    diff = _git(workspace, "diff", "HEAD")
    if diff is None:  # no commits yet: compare index/worktree against the empty tree
        diff = _git(workspace, "diff")
    if diff is None:
        return None, "Could not compute the working diff."
    if not diff.strip():
        return None, "No changes in the working tree — nothing to review."
    if len(diff) > _MAX_DIFF_CHARS:
        truncated = diff[:_MAX_DIFF_CHARS] + "\n… [diff truncated]"
        return truncated, "Reviewing a truncated working diff:"
    return diff, "Reviewing the working diff:"


# -- output shaping -----------------------------------------------------------


def _format_findings(text: str) -> str:
    if not text:
        return "Review returned no output."
    if text.strip().upper() == _NO_FINDINGS:
        return "Review: no bug findings in the working diff."
    findings: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        for bullet in ("- ", "* ", "+ "):
            if line.startswith(bullet):
                line = line[len(bullet) :].strip()
                break
        if line and _FINDING_RE.match(line):
            findings.append(line)
    if not findings:
        return "Review (unstructured — the model did not return findings lines):\n" + text
    noun = "finding" if len(findings) == 1 else "findings"
    return "\n".join([f"Review — {len(findings)} {noun}:", *(f"  {f}" for f in findings)])


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("review", "review the working diff for bugs", "/review", _cmd_review),
)
