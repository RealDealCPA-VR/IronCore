"""Read-side fs tools contract (IC-301): honest truncation, deterministic output."""

import asyncio

from ironcore.safety.risk import ToolRisk
from ironcore.tools.fs_read import (
    DEFAULT_READ_LIMIT,
    MAX_GLOB_MATCHES,
    MAX_GREP_MATCHES,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
)


def run(tool, **kwargs):
    return asyncio.run(tool.run(**kwargs))


# --- contract metadata -------------------------------------------------------


def test_all_tools_declare_read_risk_and_model_facing_specs(tmp_path):
    tools = [ReadFileTool(tmp_path), ListDirTool(tmp_path), GlobTool(tmp_path), GrepTool(tmp_path)]
    assert [t.name for t in tools] == ["read_file", "list_dir", "glob", "grep"]
    for tool in tools:
        assert tool.risk is ToolRisk.READ
        assert "Example:" in tool.description
        spec = tool.spec()
        assert spec["function"]["name"] == tool.name
        assert spec["function"]["parameters"]["type"] == "object"
    assert ReadFileTool(tmp_path).parameters["required"] == ["path"]
    assert GlobTool(tmp_path).parameters["required"] == ["pattern"]
    assert GrepTool(tmp_path).parameters["required"] == ["pattern"]


# --- read_file ---------------------------------------------------------------


def test_read_file_line_numbers_cat_n_style(tmp_path):
    (tmp_path / "f.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    result = run(ReadFileTool(tmp_path), path="f.txt")
    assert result.ok
    assert result.output.splitlines() == ["     1\talpha", "     2\tbeta", "     3\tgamma"]


def test_read_file_offset_limit_and_exact_truncation_count(tmp_path):
    (tmp_path / "f.txt").write_text("".join(f"line{i}\n" for i in range(1, 11)), encoding="utf-8")
    result = run(ReadFileTool(tmp_path), path="f.txt", offset=3, limit=4)
    assert result.ok
    lines = result.output.splitlines()
    assert lines[0] == "     3\tline3"
    assert lines[3] == "     6\tline6"
    # 10 total, showed 3..6 -> exactly 4 remain.
    assert lines[4] == "... [truncated: 4 more lines]"
    assert len(lines) == 5


def test_read_file_default_limit_truncates_honestly(tmp_path):
    n = DEFAULT_READ_LIMIT + 37
    (tmp_path / "big.txt").write_text("".join(f"{i}\n" for i in range(n)), encoding="utf-8")
    result = run(ReadFileTool(tmp_path), path="big.txt")
    assert result.ok
    lines = result.output.splitlines()
    assert len(lines) == DEFAULT_READ_LIMIT + 1
    assert lines[-1] == "... [truncated: 37 more lines]"


def test_read_file_crlf_lines_have_no_trailing_cr(tmp_path):
    (tmp_path / "dos.txt").write_bytes(b"one\r\ntwo\r\n")
    result = run(ReadFileTool(tmp_path), path="dos.txt")
    assert result.ok
    assert result.output.splitlines() == ["     1\tone", "     2\ttwo"]


def test_read_file_missing_returns_error_not_exception(tmp_path):
    result = run(ReadFileTool(tmp_path), path="nope.txt")
    assert not result.ok
    assert "nope.txt" in result.error


def test_read_file_on_directory_returns_error(tmp_path):
    (tmp_path / "adir").mkdir()
    result = run(ReadFileTool(tmp_path), path="adir")
    assert not result.ok
    assert result.error


def test_read_file_offset_past_end_is_honest(tmp_path):
    (tmp_path / "f.txt").write_text("only\n", encoding="utf-8")
    result = run(ReadFileTool(tmp_path), path="f.txt", offset=99)
    assert result.ok
    assert "1 lines" in result.output and "99" in result.output


def test_read_file_empty_file(tmp_path):
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    result = run(ReadFileTool(tmp_path), path="empty.txt")
    assert result.ok
    assert result.output == "(empty file)"


def test_read_file_binary_refused_without_exception(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"MZ\x00\x01payload")
    result = run(ReadFileTool(tmp_path), path="blob.bin")
    assert not result.ok
    assert "binary" in result.error


def test_read_file_bad_offset_and_limit(tmp_path):
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    assert not run(ReadFileTool(tmp_path), path="f.txt", offset=0).ok
    assert not run(ReadFileTool(tmp_path), path="f.txt", limit=0).ok


# --- list_dir ----------------------------------------------------------------


def test_list_dir_dirs_first_then_files_sorted(tmp_path):
    (tmp_path / "zeta").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "m.py").write_text("m", encoding="utf-8")
    result = run(ListDirTool(tmp_path))  # default path "."
    assert result.ok
    assert result.output.splitlines() == ["alpha/", "zeta/", "m.py", "z.txt"]


def test_list_dir_subdir_and_empty(tmp_path):
    (tmp_path / "sub").mkdir()
    result = run(ListDirTool(tmp_path), path="sub")
    assert result.ok
    assert result.output == "(empty directory)"


def test_list_dir_missing_and_not_a_directory(tmp_path):
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    assert not run(ListDirTool(tmp_path), path="ghost").ok
    result = run(ListDirTool(tmp_path), path="f.txt")
    assert not result.ok
    assert "not a directory" in result.error


# --- glob --------------------------------------------------------------------


def test_glob_recursive_matches_sorted_relative_posix(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "b.py").write_text("b", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "src" / "note.txt").write_text("t", encoding="utf-8")
    result = run(GlobTool(tmp_path), pattern="**/*.py", path="src")
    assert result.ok
    assert result.output.splitlines() == ["src/b.py", "src/pkg/a.py"]


def test_glob_matches_files_not_directories(tmp_path):
    (tmp_path / "thing").mkdir()
    (tmp_path / "thing.txt").write_text("x", encoding="utf-8")
    result = run(GlobTool(tmp_path), pattern="thing*")
    assert result.ok
    assert result.output.splitlines() == ["thing.txt"]


def test_glob_cap_reports_exact_remaining(tmp_path):
    for i in range(MAX_GLOB_MATCHES + 5):
        (tmp_path / f"f{i:04d}.txt").write_text("", encoding="utf-8")
    result = run(GlobTool(tmp_path), pattern="*.txt")
    assert result.ok
    lines = result.output.splitlines()
    assert len(lines) == MAX_GLOB_MATCHES + 1
    assert lines[-1] == "... [truncated: 5 more matches]"
    assert lines[0] == "f0000.txt"  # sorted


def test_glob_no_matches_and_missing_dir(tmp_path):
    result = run(GlobTool(tmp_path), pattern="*.zig")
    assert result.ok
    assert result.output == "(no matches)"
    assert not run(GlobTool(tmp_path), pattern="*", path="ghost").ok


# --- grep --------------------------------------------------------------------


def test_grep_reports_relpath_lineno_line(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import os\n\ndef main():\n", encoding="utf-8")
    (tmp_path / "readme.md").write_text("run main() to start\n", encoding="utf-8")
    result = run(GrepTool(tmp_path), pattern=r"def main")
    assert result.ok
    assert result.output.splitlines() == ["src/app.py:3:def main():"]


def test_grep_skips_binary_file_silently(tmp_path):
    (tmp_path / "hit.txt").write_text("needle here\n", encoding="utf-8")
    # Planted binary contains the pattern bytes AND a null byte: must be skipped.
    (tmp_path / "blob.bin").write_bytes(b"needle\x00needle\xff\xfe")
    result = run(GrepTool(tmp_path), pattern="needle")
    assert result.ok
    assert result.output.splitlines() == ["hit.txt:1:needle here"]


def test_grep_glob_filter_limits_files(tmp_path):
    (tmp_path / "a.py").write_text("target\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("target\n", encoding="utf-8")
    result = run(GrepTool(tmp_path), pattern="target", glob="*.py")
    assert result.ok
    assert result.output.splitlines() == ["a.py:1:target"]


def test_grep_single_file_path(tmp_path):
    (tmp_path / "one.txt").write_text("x\nmatch me\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("match me too\n", encoding="utf-8")
    result = run(GrepTool(tmp_path), pattern="match", path="one.txt")
    assert result.ok
    assert result.output.splitlines() == ["one.txt:2:match me"]


def test_grep_cap_reports_exact_remaining(tmp_path):
    n = MAX_GREP_MATCHES + 10
    (tmp_path / "many.txt").write_text("".join(f"hit {i}\n" for i in range(n)), encoding="utf-8")
    result = run(GrepTool(tmp_path), pattern="hit")
    assert result.ok
    lines = result.output.splitlines()
    assert len(lines) == MAX_GREP_MATCHES + 1
    assert lines[-1] == "... [truncated: 10 more matches]"


def test_grep_invalid_regex_returns_error_not_exception(tmp_path):
    result = run(GrepTool(tmp_path), pattern="([")
    assert not result.ok
    assert "regex" in result.error


def test_grep_no_matches_and_missing_path(tmp_path):
    (tmp_path / "f.txt").write_text("nothing\n", encoding="utf-8")
    result = run(GrepTool(tmp_path), pattern="absent_zz")
    assert result.ok
    assert result.output == "(no matches)"
    assert not run(GrepTool(tmp_path), pattern="x", path="ghost").ok
