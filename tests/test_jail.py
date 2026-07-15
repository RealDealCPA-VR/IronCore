"""The path jail is the T2 control — every escape class raises, in every mode, on every OS."""

import os
from pathlib import Path

import pytest

from ironcore.safety.jail import JailViolation, is_inside, resolve_jailed

WINDOWS = os.name == "nt"


@pytest.fixture
def ws(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


def _symlink_or_skip(link: Path, target: Path) -> None:
    # Windows without Developer Mode / admin lacks the symlink privilege.
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")


def _escape_attempts(ws: Path) -> list[str]:
    """Escape candidates. Core forms run on both OSes; OS-specific spellings are guarded."""
    attempts = [
        # .. traversal
        "..",
        "../..",
        "../outside.txt",
        "../../etc/passwd",
        "a/../../b",
        "a/b/../../../c",
        "./../sneak",
        "../sibling/file.txt",
        "../" * 20 + "deep-escape",  # clamps at the filesystem root — still outside
        # absolute escapes
        str(ws.parent),
        str(ws.parent / "elsewhere"),
        str(Path.home()),  # an ANCESTOR of tmp workspaces — outside, not inside
        str(ws) + "-suffix/file.txt",  # sibling whose name merely startswith(workspace)
    ]
    if WINDOWS:
        attempts += [
            # drive-letter absolute
            "C:\\Windows",
            "C:\\Windows\\System32\\drivers\\etc\\hosts",
            "D:\\data\\other-drive.txt",
            # UNC / extended-length / device namespace
            "\\\\server\\share\\loot",
            "\\\\?\\C:\\Windows",
            "\\\\.\\C:\\Windows",
            # CWD-dependent forms: drive without root, root without drive
            "C:evil.txt",
            "\\Windows\\evil",
            # Win32 trailing dot/space rewrites (".. " opens as "..")
            ".. ",
            ".. \\up.txt",
            "...",
            "sub\\file. ",
        ]
    else:
        attempts += [
            "/etc/passwd",
            "/root/.bashrc",
            "/",
            "//server/share/loot",  # POSIX double-slash root is absolute
            "/tmp",  # ancestor (or unrelated) — never inside a tmp workspace
        ]
    return attempts


LEGIT = [
    "subdir/file.py",
    "./a/b",
    "deeply/nested/ok.txt",
    ".",
    "a/../b",  # traversal that stays inside
    "sub/./x.txt",
    "a.b.c/d.e",  # interior dots are fine
    "dir with space/file.txt",  # interior spaces are fine
]


def test_jail_violation_is_a_value_error():
    assert issubclass(JailViolation, ValueError)


def test_escape_table_is_big_enough(ws):
    assert len(_escape_attempts(ws)) >= 15


def test_every_escape_attempt_raises(ws):
    not_caught = []
    for raw in _escape_attempts(ws):
        try:
            resolve_jailed(ws, raw)
        except JailViolation:
            continue
        not_caught.append(raw)
    assert not_caught == []


def test_is_inside_is_false_for_every_escape(ws):
    leaked = [raw for raw in _escape_attempts(ws) if is_inside(ws, raw)]
    assert leaked == []


def test_legit_nested_paths_resolve_inside(ws):
    root = ws.resolve()
    for raw in LEGIT:
        resolved = resolve_jailed(ws, raw)
        assert resolved.is_absolute()
        assert resolved.is_relative_to(root), raw
        assert is_inside(ws, raw), raw


def test_resolved_value_is_the_joined_path(ws):
    assert resolve_jailed(ws, "subdir/file.py") == ws.resolve() / "subdir" / "file.py"
    # Path objects work the same as strings
    assert resolve_jailed(ws, Path("subdir/file.py")) == ws.resolve() / "subdir" / "file.py"


def test_workspace_root_itself_is_inside(ws):
    assert resolve_jailed(ws, ws) == ws.resolve()
    assert resolve_jailed(ws, ".") == ws.resolve()
    assert is_inside(ws, ws)


def test_absolute_path_inside_is_allowed(ws):
    inside = ws / "abs" / "inside.txt"
    assert resolve_jailed(ws, inside) == inside.resolve()
    assert is_inside(ws, str(inside))


def test_relative_candidates_anchor_at_workspace_not_cwd(ws, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # CWD is OUTSIDE the workspace
    assert resolve_jailed(ws, "sub/f.txt") == ws.resolve() / "sub" / "f.txt"


def test_nul_byte_is_a_violation(ws):
    with pytest.raises(JailViolation):
        resolve_jailed(ws, "bad\x00null")


def test_is_inside_never_raises_on_garbage(ws):
    assert is_inside(ws, "bad\x00null") is False
    assert is_inside(ws, 42) is False  # TypeError swallowed: predicate form never raises


@pytest.mark.skipif(not WINDOWS, reason="Windows path semantics")
def test_windows_containment_is_case_insensitive(ws):
    swapped = str(ws).swapcase() + "\\sub\\f.txt"
    assert is_inside(ws, swapped)


# --- symlink escapes (skipped where symlink creation is unavailable) -------------------


def test_dir_symlink_escape_is_rejected(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    _symlink_or_skip(ws / "innocent", outside)
    with pytest.raises(JailViolation):
        resolve_jailed(ws, "innocent")  # the link itself resolves outside
    with pytest.raises(JailViolation):
        resolve_jailed(ws, "innocent/loot.txt")  # and so does everything under it
    assert not is_inside(ws, "innocent/loot.txt")


def test_file_symlink_escape_is_rejected(ws, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("s3cret")
    _symlink_or_skip(ws / "readme.txt", secret)
    with pytest.raises(JailViolation):
        resolve_jailed(ws, "readme.txt")


def test_symlink_inside_to_inside_is_allowed(ws):
    real = ws / "real"
    real.mkdir()
    _symlink_or_skip(ws / "alias", real)
    assert resolve_jailed(ws, "alias/file.txt") == (real / "file.txt").resolve()


def test_symlinked_workspace_is_resolved_before_containment(tmp_path):
    real_ws = tmp_path / "real_ws"
    real_ws.mkdir()
    ws_link = tmp_path / "ws_link"
    _symlink_or_skip(ws_link, real_ws)
    resolved = resolve_jailed(ws_link, "sub/file.txt")
    assert resolved == (real_ws / "sub" / "file.txt").resolve()
    assert is_inside(ws_link, "sub/file.txt")
    # and escapes still escape through the symlinked root
    with pytest.raises(JailViolation):
        resolve_jailed(ws_link, "../outside.txt")
