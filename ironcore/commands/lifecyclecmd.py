"""/compact, /undo, /redo (IC-805): compaction + byte-exact change-set revert.

``/compact`` distills the engine's running conversation into one handoff-grade
summary + the recent tail (SPEC §11.2). Summarization calls the provider, so it
is ASYNC: the handler returns an ack and posts the result via ``schedule``,
mirroring the engine's own auto-compaction (``core.compact.compact`` +
``should_compact``).

``/undo`` and ``/redo`` drive ``ironcore.safety.snapshots.SnapshotStore`` — the
shadow-git store that restores the whole workspace byte-exactly (SPEC §7.6).
Snapshot ops are quick, self-contained git plumbing, so they run INLINE (sync),
and every failure mode (git missing, no snapshots, workspace changed since undo)
degrades to a clear message.
"""

from __future__ import annotations

from ironcore.commands._helpers import resolve_workspace
from ironcore.commands.base import CommandContext, SlashCommand
from ironcore.core.compact import compact, should_compact
from ironcore.safety.snapshots import SnapshotError, SnapshotStore

#: Messages kept verbatim after the summary — mirrors engine._KEEP_RECENT.
_KEEP_RECENT = 6


# -- /compact -----------------------------------------------------------------


def _cmd_compact(ctx: CommandContext, args: str) -> str:
    engine = ctx.extra.get("engine")
    if engine is None:
        return "Compaction needs a live session (no engine available here)."
    conversation = getattr(engine, "_conversation", None)
    if not conversation:
        return "Nothing to compact — the conversation is empty."
    schedule = ctx.extra.get("schedule")
    if schedule is None:
        return "Compaction needs the scheduler (not available here)."

    profile = getattr(engine, "profile", None)
    note = ""
    if profile is not None and not should_compact(conversation, profile=profile):
        note = " (history is under the context budget; compacting on request)"
    schedule(_compact_coro(engine, ctx.settings))
    return f"Compacting {len(conversation)} message(s) into a handoff-grade summary…{note}"


async def _compact_coro(engine, settings) -> str:
    conversation = getattr(engine, "_conversation", None)
    if not conversation:
        return "Nothing to compact."
    original = len(conversation)
    summary = await compact(
        conversation,
        provider=engine.provider,
        model=settings.roles.summarizer or "",
    )
    engine._conversation = [summary, *conversation[-_KEEP_RECENT:]]
    kept = len(engine._conversation) - 1
    return f"Compacted {original} message(s) into 1 summary + {kept} recent message(s)."


# -- /undo and /redo ----------------------------------------------------------


def _cmd_undo(ctx: CommandContext, args: str) -> str:
    return _revert(ctx, redo=False)


def _cmd_redo(ctx: CommandContext, args: str) -> str:
    return _revert(ctx, redo=True)


def _revert(ctx: CommandContext, *, redo: bool) -> str:
    verb = "redo" if redo else "undo"
    workspace = resolve_workspace(ctx)
    if workspace is None:
        return f"No workspace available for {verb}."
    try:
        store = SnapshotStore(workspace)
        snapshots = store.list_snapshots()
    except SnapshotError as exc:
        return f"Cannot {verb}: {exc}"
    if not snapshots:
        return f"No snapshots yet — nothing to {verb}."

    labels = {commit: label for commit, label in snapshots}
    try:
        target = store.redo() if redo else store.undo()
    except SnapshotError as exc:
        return f"{verb.capitalize()} failed: {exc}"

    if target is None:
        if redo:
            return "Nothing to redo (or the workspace changed since the undo — new work is kept)."
        return "Nothing to undo."

    label = labels.get(target)
    if label is None:
        label = next((lbl for cid, lbl in store.list_snapshots() if cid == target), "(restored)")
    action = "Reapplied" if redo else "Reverted to"
    return f"{action} snapshot {target[:8]}: {label}"


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        "compact", "compress history into a handoff-grade summary", "/compact", _cmd_compact
    ),
    SlashCommand("undo", "revert the last change set (git snapshots)", "/undo", _cmd_undo),
    SlashCommand("redo", "reapply the last reverted change set", "/redo", _cmd_redo),
)
