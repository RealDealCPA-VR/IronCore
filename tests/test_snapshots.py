"""SnapshotStore pins: byte-exact undo/redo in git and non-git workspaces (SPEC §7.6)."""

import subprocess
from pathlib import Path

import pytest

from ironcore.safety.snapshots import UNDO_REF, SnapshotError, SnapshotStore

# every test-side git call carries local identity + deterministic line endings
# so commits work in CI and byte-exactness holds on Windows
GIT_FLAGS = [
    "-c", "user.email=ci@example.invalid",
    "-c", "user.name=IronCore CI",
    "-c", "core.autocrlf=false",
    "-c", "commit.gpgsign=false",
]

ORIG = {
    "keep.txt": b"keep line1\r\nkeep line2\n\x00\xffbinary tail",
    "gone.txt": b"delete me\n",
    "sub/nested.txt": b"nested contents\n",
    ".gitignore": b"ignored.log\n",
    "ignored.log": b"ignored v1\n",
}


def git(ws: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ws), *GIT_FLAGS, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def seed(ws: Path) -> None:
    for rel, data in ORIG.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def make_user_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "user-ws"
    ws.mkdir()
    seed(ws)
    git(ws, "init", "-q")
    git(ws, "add", ".")
    git(ws, "commit", "-q", "-m", "base")
    (ws / "untracked.txt").write_bytes(b"untracked but real\n")  # must be captured too
    return ws


def make_plain_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "plain-ws"
    ws.mkdir()
    seed(ws)
    (ws / "untracked.txt").write_bytes(b"untracked but real\n")
    return ws


def mutate(ws: Path) -> None:
    (ws / "keep.txt").write_bytes(b"EDITED\n")  # edit
    (ws / "new.txt").write_bytes(b"brand new\n")  # add
    (ws / "gone.txt").unlink()  # delete
    (ws / "sub" / "nested.txt").unlink()  # delete nested
    (ws / "untracked.txt").unlink()  # delete an untracked file
    (ws / "ignored.log").write_bytes(b"ignored v2\n")  # ignored: outside snapshot scope


def tree_bytes(ws: Path) -> dict[str, bytes]:
    """Every file in the workspace (minus .git/.ironcore) as {posix relpath: bytes}."""
    files: dict[str, bytes] = {}
    for path in ws.rglob("*"):
        rel = path.relative_to(ws).as_posix()
        if rel.split("/", 1)[0] in (".git", ".ironcore"):
            continue
        if path.is_file():
            files[rel] = path.read_bytes()
    return files


def roundtrip(ws: Path) -> SnapshotStore:
    """snapshot → mutate → undo (byte-exact back) → redo (byte-exact forward)."""
    store = SnapshotStore(ws)
    marker = ws / ".ironcore" / "probe.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"ironcore state, hands off")
    before = tree_bytes(ws)
    sid = store.snapshot("before-mutation")
    int(sid, 16)  # a real git object id
    mutate(ws)
    after = tree_bytes(ws)
    assert after != before
    assert store.undo() == sid
    # ignored.log was never captured, so undo leaves its mutated bytes alone
    assert tree_bytes(ws) == {**before, "ignored.log": b"ignored v2\n"}
    redone = store.redo()
    assert redone is not None and redone != sid
    assert tree_bytes(ws) == after
    assert marker.read_bytes() == b"ironcore state, hands off"  # .ironcore/ untouched
    assert store.list_snapshots()[0] == (sid, "before-mutation")
    return store


def test_user_git_roundtrip(tmp_path):
    ws = make_user_ws(tmp_path)
    store = roundtrip(ws)
    assert store.backend == "user-git"


def test_non_git_roundtrip(tmp_path):
    ws = make_plain_ws(tmp_path)
    store = roundtrip(ws)
    assert store.backend == "private"
    assert not (ws / ".git").exists()  # the workspace itself is never made a repo
    assert (ws / ".ironcore" / "snapshots" / "git" / "HEAD").exists()


def test_snapshot_transparent_to_user_repo(tmp_path):
    ws = make_user_ws(tmp_path)
    # a live session dir already exists (audit writes there), so `?? .ironcore/`
    # appears in both probes — snapshot() itself must add nothing
    audit_line = ws / ".ironcore" / "audit" / "2026-07-15.jsonl"
    audit_line.parent.mkdir(parents=True)
    audit_line.write_bytes(b"{}\n")
    (ws / "keep.txt").write_bytes(b"staged change\n")
    git(ws, "add", "keep.txt")  # a real staged entry in the USER'S index
    (ws / "gone.txt").write_bytes(b"unstaged change\n")

    def probe() -> tuple[str, str, str, str]:
        return (
            git(ws, "status", "--porcelain"),
            git(ws, "rev-parse", "--abbrev-ref", "HEAD"),
            git(ws, "rev-parse", "HEAD"),
            git(ws, "for-each-ref", "refs/heads", "--format=%(refname)"),
        )

    store = SnapshotStore(ws)
    before = probe()
    sid = store.snapshot("transparent")
    assert probe() == before  # status/branch/HEAD/branches byte-identical
    assert git(ws, "rev-parse", UNDO_REF) == sid  # only the dedicated ref moved
    names = git(ws, "ls-tree", "-r", "--name-only", sid).splitlines()
    assert names and all(not n.startswith(".ironcore") for n in names)
    assert store.undo() is None  # nothing before the only snapshot; still transparent
    assert probe() == before


def test_multi_step_undo_redo_chain(tmp_path):
    ws = make_plain_ws(tmp_path)
    store = SnapshotStore(ws)
    f = ws / "keep.txt"
    f.write_bytes(b"v1")
    id1 = store.snapshot("v1")
    f.write_bytes(b"v2")
    id2 = store.snapshot("v2")
    f.write_bytes(b"v3")
    id3 = store.snapshot("v3")
    assert store.undo() == id2 and f.read_bytes() == b"v2"
    assert store.undo() == id1 and f.read_bytes() == b"v1"
    assert store.undo() is None  # bottom of the stack
    assert store.redo() == id2 and f.read_bytes() == b"v2"
    assert store.redo() == id3 and f.read_bytes() == b"v3"
    assert store.redo() is None  # top of the stack
    assert [label for _, label in store.list_snapshots()] == ["v1", "v2", "v3"]


def test_undo_banks_dirty_state_for_redo(tmp_path):
    ws = make_plain_ws(tmp_path)
    store = SnapshotStore(ws)
    sid = store.snapshot("clean")
    mutate(ws)
    dirty = tree_bytes(ws)
    assert store.undo() == sid  # dirty state auto-banked, then clean restored
    rid = store.redo()
    assert rid is not None
    assert tree_bytes(ws) == dirty  # the never-explicitly-snapshotted state came back
    entries = store.list_snapshots()
    assert entries[0] == (sid, "clean")
    assert entries[1][0] == rid  # the automatic pre-undo snapshot


def test_empty_store_has_nothing_to_undo(tmp_path):
    ws = make_plain_ws(tmp_path)
    store = SnapshotStore(ws)
    assert store.undo() is None
    assert store.redo() is None
    assert store.list_snapshots() == []
    assert not (ws / ".ironcore").exists()  # no-ops leave no droppings


def test_redo_refuses_after_new_changes(tmp_path):
    ws = make_plain_ws(tmp_path)
    store = SnapshotStore(ws)
    store.snapshot("a")
    (ws / "keep.txt").write_bytes(b"B")
    assert store.undo() is not None
    (ws / "keep.txt").write_bytes(b"C")  # new work after the undo
    assert store.redo() is None  # clobbering C would be data loss, not redo
    assert (ws / "keep.txt").read_bytes() == b"C"


def test_git_missing_raises_snapshot_error(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    with pytest.raises(SnapshotError, match="git"):
        SnapshotStore(ws)


def test_missing_workspace_raises(tmp_path):
    with pytest.raises(SnapshotError, match="workspace"):
        SnapshotStore(tmp_path / "does-not-exist")
