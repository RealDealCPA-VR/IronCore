"""Shadow-git snapshot store: byte-exact undo/redo (docs/SAFETY.md §5, SPEC §7.6).

Snapshots capture the FULL workspace file state — tracked, plus untracked but
not ignored — and restore it byte-exactly: edited files revert, added files
are removed, deleted files come back. Rules this module enforces:

- Stdlib only (safety package rule, docs/ARCHITECTURE.md §4): git is driven
  via ``subprocess``; GitPython must never be added.
- The user's git state is sacred. In a git workspace, snapshots are commits
  on the dedicated ref ``refs/ironcore/undo`` — never a branch — built through
  a PRIVATE index file (``GIT_INDEX_FILE`` under ``.ironcore/snapshots/``), so
  the user's staging index, HEAD, branch, and ``git status`` are untouched by
  ``snapshot()``/``undo()``/``redo()`` aside from the intended worktree file
  changes a restore performs. IronCore never edits the user's config,
  excludes, or attributes.
- Non-git workspaces get a private bare repo at ``.ironcore/snapshots/git``
  whose work-tree is the workspace: identical mechanics, fully self-owned.
- ``.ironcore/`` itself is never captured and never deleted by a restore;
  files matched by the workspace's .gitignore are neither captured nor
  deleted (a restore leaves ignored files exactly as they are).
- ``core.autocrlf=false`` is forced on every git invocation so round-trips
  are byte-exact on Windows. In-tree .gitattributes content filters (e.g.
  LFS) are the one thing that can break exactness; IronCore does not rewrite
  the user's attributes to work around them.
- One writer per workspace: the undo cursor lives in state.json next to the
  private index (written atomically); concurrent sessions are not arbitrated.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import uuid
from pathlib import Path

#: The dedicated ref all snapshot commits live on (docs/SAFETY.md §5). Never a branch.
UNDO_REF = "refs/ironcore/undo"

#: Label of the automatic snapshot undo() takes of a dirty workspace so redo() can return to it.
AUTO_UNDO_LABEL = "auto: workspace state before undo"

#: Snapshot commits carry IronCore's own identity — works even where the user set none.
_IDENT = {
    "GIT_AUTHOR_NAME": "IronCore",
    "GIT_AUTHOR_EMAIL": "undo@ironcore.invalid",
    "GIT_COMMITTER_NAME": "IronCore",
    "GIT_COMMITTER_EMAIL": "undo@ironcore.invalid",
}

#: Inherited env that would silently repoint git at the wrong repo/index — always stripped.
_UNSAFE_GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)

#: Characters of git stderr carried into a SnapshotError message.
_STDERR_CAP = 400


class SnapshotError(Exception):
    """git is missing, a git command failed, or the undo state is unreadable."""


class SnapshotStore:
    """Byte-exact undo/redo of workspace change sets via shadow git commits.

    ``backend`` is ``"user-git"`` (the workspace is a git work tree; snapshot
    commits land in the user's object store on UNDO_REF through a private
    index, leaving their index/HEAD/branches alone) or ``"private"`` (non-git
    workspace; a private repo under ``.ironcore/snapshots/`` uses the
    workspace as its work-tree).
    """

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise SnapshotError(f"workspace is not a directory: {self.workspace}")
        self._dir = self.workspace / ".ironcore" / "snapshots"
        self._index = self._dir / "index"
        self._state_path = self._dir / "state.json"
        self._git_dir = self._dir / "git"
        self.backend = self._detect_backend()

    # ── public API ────────────────────────────────────────────────────────

    def snapshot(self, label: str) -> str:
        """Capture the current workspace state; return the snapshot commit id.

        Taking a snapshot after undos discards the redo tail (standard undo-
        stack semantics) — the discarded commits stay reachable on UNDO_REF.
        """
        tree = self._capture_tree()
        commit = self._commit_snapshot(tree, str(label))
        state = self._load_state()
        del state["entries"][state["cursor"] + 1 :]
        state["entries"].append({"id": commit, "tree": tree, "label": str(label)})
        state["cursor"] = len(state["entries"]) - 1
        self._save_state(state)
        return commit

    def undo(self) -> str | None:
        """Restore the snapshot before the latest; return its id (None = nothing to undo).

        If the workspace changed since the last snapshot/restore, that dirty
        state is first banked as an automatic snapshot so ``redo()`` can bring
        it back byte-exactly.
        """
        state = self._load_state()
        entries, cursor = state["entries"], state["cursor"]
        if not entries:
            return None
        tree = self._capture_tree()
        if tree != entries[cursor]["tree"]:
            del entries[cursor + 1 :]  # divergence invalidates the old redo tail
            commit = self._commit_snapshot(tree, AUTO_UNDO_LABEL)
            entries.append({"id": commit, "tree": tree, "label": AUTO_UNDO_LABEL})
            cursor = len(entries) - 1
            state["cursor"] = cursor
            self._save_state(state)  # bank the dirty state before touching files
        if cursor == 0:
            return None
        target = entries[cursor - 1]
        self._restore(target["id"])
        state["cursor"] = cursor - 1
        self._save_state(state)
        return target["id"]

    def redo(self) -> str | None:
        """Re-apply the snapshot after the current one; None if nothing to redo.

        Refuses (returns None) if the workspace changed since the undo —
        silently clobbering new work would be data loss, not redo.
        """
        state = self._load_state()
        entries, cursor = state["entries"], state["cursor"]
        if cursor >= len(entries) - 1:
            return None
        if self._capture_tree() != entries[cursor]["tree"]:
            return None
        target = entries[cursor + 1]
        self._restore(target["id"])
        state["cursor"] = cursor + 1
        self._save_state(state)
        return target["id"]

    def list_snapshots(self) -> list[tuple[str, str]]:
        """All snapshots, oldest first, as ``(commit id, label)`` pairs."""
        return [(e["id"], e["label"]) for e in self._load_state()["entries"]]

    # ── git plumbing ──────────────────────────────────────────────────────

    def _detect_backend(self) -> str:
        proc = self._exec(["git", "rev-parse", "--is-inside-work-tree"], check=False)
        inside = proc.returncode == 0 and proc.stdout.decode("ascii", "replace").strip() == "true"
        return "user-git" if inside else "private"

    def _ensure_ready(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if self.backend == "private" and not (self._git_dir / "HEAD").exists():
            self._exec(
                ["git", "-c", "init.defaultBranch=ironcore", "init", "-q", "--bare",
                 str(self._git_dir)]
            )
            # detached-work-tree pattern: a bare gitdir driven with an explicit
            # --work-tree; core.bare=false keeps every worktree command happy
            self._exec(["git", "--git-dir", str(self._git_dir), "config", "core.bare", "false"])

    def _capture_tree(self) -> str:
        """Sync the private index to the workspace and return its tree id."""
        self._ensure_ready()
        # -A against the persistent private index records adds/edits/deletes in
        # one pass (and keeps git's stat cache warm); the pathspec keeps
        # .ironcore/ out of every snapshot; .gitignore applies as usual
        self._run(["add", "-A", "--", ".", ":(exclude).ironcore"])
        return self._run(["write-tree"]).stdout.decode("ascii").strip()

    def _commit_snapshot(self, tree: str, label: str) -> str:
        parent = self._ref_tip()
        # nonce ⇒ unique commit ids even for identical trees in the same second
        message = f"{label}\n\nironcore-snapshot: {uuid.uuid4().hex}\n"
        args = ["commit-tree", tree, "-m", message]
        if parent is not None:
            args[2:2] = ["-p", parent]
        commit = self._run(args).stdout.decode("ascii").strip()
        # only the dedicated ref advances — never HEAD, never a branch
        self._run(["update-ref", UNDO_REF, commit])
        return commit

    def _ref_tip(self) -> str | None:
        proc = self._run(["rev-parse", "-q", "--verify", UNDO_REF], check=False)
        return proc.stdout.decode("ascii").strip() if proc.returncode == 0 else None

    def _restore(self, commit: str) -> None:
        """Make the workspace byte-identical to ``commit`` (except .ironcore/, ignored)."""
        self._ensure_ready()
        self._run(["read-tree", commit])  # private index := snapshot tree
        # files present now but absent from the snapshot (and not ignored) must
        # go; deletion happens before checkout so file/dir swaps cannot collide
        listed = self._run(["ls-files", "-z", "--others", "--exclude-standard"])
        extraneous: list[Path] = []
        for raw in listed.stdout.split(b"\0"):
            if not raw:
                continue
            rel = raw.decode("utf-8", "replace")
            if rel == ".ironcore" or rel.startswith(".ironcore/"):
                continue  # never touch IronCore's own state dir
            extraneous.append(self.workspace / Path(rel))
        for path in extraneous:
            self._delete_file(path)
        self._prune_empty_dirs(extraneous)
        self._run(["checkout-index", "-a", "-f"])  # write every snapshot file, overwriting

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        """Run git against the right repo with the private index and forced identity."""
        cmd = ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false"]
        if self.backend == "private":
            cmd += ["--git-dir", str(self._git_dir), "--work-tree", str(self.workspace)]
        return self._exec(cmd + args, check=check)

    def _exec(
        self, cmd: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        env = {k: v for k, v in os.environ.items() if k not in _UNSAFE_GIT_ENV}
        env.update(_IDENT)
        env["GIT_INDEX_FILE"] = str(self._index)  # the user's real index is never ours
        try:
            proc = subprocess.run(cmd, cwd=str(self.workspace), env=env, capture_output=True)
        except FileNotFoundError as exc:
            raise SnapshotError("git executable not found — snapshots require git on PATH") from exc
        except OSError as exc:
            raise SnapshotError(f"could not run git: {exc}") from exc
        if check and proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()[:_STDERR_CAP]
            raise SnapshotError(f"git {' '.join(cmd[1:])} failed ({proc.returncode}): {err}")
        return proc

    # ── filesystem helpers ────────────────────────────────────────────────

    @staticmethod
    def _delete_file(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except PermissionError:
            # Windows: read-only files refuse unlink until writable
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            path.unlink()

    def _prune_empty_dirs(self, deleted: list[Path]) -> None:
        # git tracks no empty dirs, so dirs emptied by deletions disappear too
        for path in deleted:
            parent = path.parent
            while parent != self.workspace:
                try:
                    parent.rmdir()
                except OSError:  # not empty / already gone / in use — stop climbing
                    break
                parent = parent.parent

    # ── undo-stack state ──────────────────────────────────────────────────

    def _load_state(self) -> dict:
        if not self._state_path.exists():
            return {"entries": [], "cursor": -1}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            entries = list(raw["entries"])
            cursor = int(raw["cursor"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SnapshotError(f"snapshot state is unreadable: {self._state_path}") from exc
        # fail closed on nonsense rather than restoring the wrong thing
        if entries and not 0 <= cursor < len(entries):
            raise SnapshotError(f"snapshot cursor out of range in {self._state_path}")
        if not entries:
            cursor = -1
        return {"entries": entries, "cursor": cursor}

    def _save_state(self, state: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self._state_path)  # atomic on the same volume
