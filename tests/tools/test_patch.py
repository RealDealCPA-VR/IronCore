"""Deterministic patcher + write-side tools (IC-302).

Covers the three pure appliers (fuzzy unified diff, exactly-once
search/replace, whole-file with size guard + no-op detection), line-ending
preservation, and the WRITE tools' jail routing, atomic writes, and the
nothing-written-on-failure guarantee.
"""

import asyncio

import pytest

from ironcore.safety.risk import ToolRisk
from ironcore.tools.fs_write import EDIT_FORMATS, EditFileTool, WriteFileTool
from ironcore.tools.patch import (
    HUNK_FUZZ_LINES,
    WHOLE_FILE_MAX_CHARS,
    PatchResult,
    apply_search_replace,
    apply_unified_diff,
    apply_whole_file,
    parse_search_replace,
)


def run(tool, **kwargs):
    return asyncio.run(tool.run(**kwargs))


def d(*lines: str) -> str:
    """Join diff/file lines with LF and a trailing newline."""
    return "\n".join(lines) + "\n"


BASE = d(*(f"line{i}" for i in range(1, 21)))  # line1 .. line20

REPLACE_LINE10 = d(
    "@@ -7,7 +7,7 @@",
    " line7",
    " line8",
    " line9",
    "-line10",
    "+LINE10",
    " line11",
    " line12",
    " line13",
)


# --- apply_unified_diff --------------------------------------------------------


@pytest.mark.parametrize("drift", [0, 1, 2, 3, -1, -2, -3])
def test_unified_diff_applies_with_offset_drift(drift):
    if drift >= 0:
        pad = "".join(f"pad{i}\n" for i in range(drift))
        original = pad + BASE
    else:
        original = "".join(f"line{i}\n" for i in range(1 - drift, 21))
    result = apply_unified_diff(original, REPLACE_LINE10)
    assert result.ok, result.reason
    assert "LINE10\n" in result.new_text
    assert "line10" not in result.new_text
    assert not result.no_op


def test_unified_diff_fails_beyond_fuzz_window_naming_the_hunk():
    pad = "".join(f"pad{i}\n" for i in range(HUNK_FUZZ_LINES + 1))
    result = apply_unified_diff(pad + BASE, REPLACE_LINE10)
    assert not result.ok
    assert result.new_text is None
    assert "hunk 1" in result.reason
    assert "@@ -7,7 +7,7 @@" in result.reason
    assert "closest match at line" in result.reason


def test_unified_diff_missing_context_reports_hunk_and_hint():
    ghost = d("@@ -1,2 +1,2 @@", " ghostA", "-ghostB", "+ghostC")
    result = apply_unified_diff(BASE, ghost)
    assert not result.ok
    assert "hunk 1" in result.reason
    assert "'ghostA'" in result.reason


def test_unified_diff_multi_hunk_with_cumulative_shift():
    diff = d(
        "@@ -2,3 +2,4 @@",
        " line2",
        "+inserted-a",
        " line3",
        " line4",
        "@@ -8,3 +9,3 @@",
        " line8",
        "-line9",
        "+LINE9",
        " line10",
    )
    result = apply_unified_diff(BASE, diff)
    assert result.ok, result.reason
    lines = result.new_text.splitlines()
    assert lines[1:4] == ["line2", "inserted-a", "line3"]
    assert lines[9] == "LINE9"


def test_unified_diff_pure_insertion_hunk():
    diff = d("@@ -3,0 +4,2 @@", "+new-a", "+new-b")
    result = apply_unified_diff(BASE, diff)
    assert result.ok, result.reason
    assert result.new_text.splitlines()[3:5] == ["new-a", "new-b"]


def test_unified_diff_tolerates_preamble_and_trailing_prose():
    wrapped = "Here is the patch:\n--- a/f.txt\n+++ b/f.txt\n" + REPLACE_LINE10 + "\nDone!\n"
    result = apply_unified_diff(BASE, wrapped)
    assert result.ok, result.reason
    assert "LINE10\n" in result.new_text


def test_unified_diff_garbage_between_hunks_is_an_error_not_a_silent_drop():
    diff = d(
        "@@ -2,2 +2,2 @@",
        " line2",
        "-line3",
        "+LINE3",
        "oops I forgot the marker",
        "@@ -8,1 +8,1 @@",
        "-line8",
        "+LINE8",
    )
    result = apply_unified_diff(BASE, diff)
    assert not result.ok
    assert "unrecognized" in result.reason


def test_unified_diff_without_hunks_fails_with_guidance():
    result = apply_unified_diff(BASE, "please just apply my changes")
    assert not result.ok
    assert "@@" in result.reason


def test_unified_diff_preserves_crlf_endings():
    original = BASE.replace("\n", "\r\n")
    result = apply_unified_diff(original, REPLACE_LINE10)  # LF diff onto CRLF file
    assert result.ok, result.reason
    assert "LINE10\r\n" in result.new_text
    assert result.new_text.count("\n") == result.new_text.count("\r\n")


def test_unified_diff_keeps_lf_files_lf():
    result = apply_unified_diff(BASE, REPLACE_LINE10)
    assert result.ok
    assert "\r" not in result.new_text


def test_unified_diff_preserves_missing_trailing_newline():
    original = "a\nb\nc"  # no final newline
    diff = d("@@ -1,3 +1,3 @@", " a", "-b", "+B", " c")
    result = apply_unified_diff(original, diff)
    assert result.ok, result.reason
    assert result.new_text == "a\nB\nc"


def test_unified_diff_no_newline_marker_strips_final_newline():
    diff = d("@@ -1,2 +1,2 @@", " a", "-b", "+B", "\\ No newline at end of file")
    result = apply_unified_diff("a\nb\n", diff)
    assert result.ok, result.reason
    assert result.new_text == "a\nB"


# --- apply_search_replace ------------------------------------------------------


def test_search_replace_unique_match_applies():
    result = apply_search_replace("a\nb\nc\n", [("b\n", "B\n")])
    assert result.ok
    assert result.new_text == "a\nB\nc\n"
    assert not result.no_op


def test_search_replace_blocks_apply_in_order():
    result = apply_search_replace("one\ntwo\nthree\n", [("one", "1"), ("three", "3")])
    assert result.ok
    assert result.new_text == "1\ntwo\n3\n"


def test_search_replace_zero_match_gives_closest_context_hint():
    result = apply_search_replace(
        "def main():\n    pass\n", [("def main( ):", "def main(x):")]
    )
    assert not result.ok
    assert result.new_text is None
    assert "not found" in result.reason
    assert "closest match at line 1" in result.reason
    assert "def main():" in result.reason


def test_search_replace_ambiguous_match_reports_line_numbers_and_never_guesses():
    original = "x\ndup\ny\ndup\n"
    result = apply_search_replace(original, [("dup", "DUP")])
    assert not result.ok
    assert result.new_text is None
    assert "ambiguous" in result.reason
    assert "lines 2, 4" in result.reason


def test_search_replace_empty_search_is_refused():
    result = apply_search_replace("a\n", [("", "x")])
    assert not result.ok
    assert "empty" in result.reason


def test_search_replace_marker_text_end_to_end():
    original = "def add(a, b):\n    return a + b\n"
    edit = d(
        "<<<<<<< SEARCH",
        "    return a + b",
        "=======",
        "    return a + b  # sum",
        ">>>>>>> REPLACE",
    )
    result = apply_search_replace(original, edit)
    assert result.ok, result.reason
    assert result.new_text == "def add(a, b):\n    return a + b  # sum\n"


def test_search_replace_preserves_crlf_endings():
    result = apply_search_replace("one\r\ntwo\r\nthree\r\n", [("two\n", "TWO\n")])
    assert result.ok, result.reason
    assert result.new_text == "one\r\nTWO\r\nthree\r\n"


def test_search_replace_no_op_flagged():
    result = apply_search_replace("a\nb\n", [("b", "b")])
    assert result.ok
    assert result.no_op


def test_parse_search_replace_tolerates_prose_and_marker_width():
    text = "Making the edit now.\n<<<<<<<< SEARCH\nold\n====\nnew\n>>>>>>>> REPLACE\nDone!\n"
    blocks, err = parse_search_replace(text)
    assert err is None
    assert blocks == [("old", "new")]


def test_parse_search_replace_unterminated_block_is_explained():
    blocks, err = parse_search_replace("<<<<<<< SEARCH\nold\n=======\nnew\n")
    assert blocks == []
    assert "REPLACE" in err


def test_parse_search_replace_no_blocks_shows_the_expected_shape():
    blocks, err = parse_search_replace("here is my edit: change a to b")
    assert blocks == []
    assert "<<<<<<< SEARCH" in err


# --- apply_whole_file ----------------------------------------------------------


def test_whole_file_replaces_content():
    result = apply_whole_file("old\n", "new\n")
    assert isinstance(result, PatchResult)
    assert result.ok
    assert result.new_text == "new\n"
    assert not result.no_op


def test_whole_file_size_guard_rejects_runaway_content():
    result = apply_whole_file("small\n", "x" * (WHOLE_FILE_MAX_CHARS + 1))
    assert not result.ok
    assert result.new_text is None
    assert "cap" in result.reason


def test_whole_file_no_op_detected_exact():
    result = apply_whole_file("same\n", "same\n")
    assert result.ok
    assert result.no_op


def test_whole_file_restores_crlf_and_detects_no_op_across_endings():
    # LF-flavored content for a CRLF file: endings are restored, and the
    # LF spelling of identical content still counts as a no-op.
    changed = apply_whole_file("a\r\nb\r\n", "a\nc\n")
    assert changed.ok
    assert changed.new_text == "a\r\nc\r\n"
    assert not changed.no_op
    same = apply_whole_file("a\r\nb\r\n", "a\nb\n")
    assert same.ok
    assert same.no_op


def test_whole_file_preserves_missing_trailing_newline_no_op():
    result = apply_whole_file("tail-less", "tail-less")
    assert result.ok
    assert result.no_op


# --- WRITE tools: contract surface ----------------------------------------------


def test_write_tools_contract_surface(tmp_path):
    tools = [WriteFileTool(tmp_path), EditFileTool(tmp_path)]
    assert [t.name for t in tools] == ["write_file", "edit_file"]
    for tool in tools:
        assert tool.risk is ToolRisk.WRITE
        assert "Example:" in tool.description
        spec = tool.spec()
        assert spec["type"] == "function"
        assert spec["function"]["name"] == tool.name
        assert spec["function"]["parameters"]["type"] == "object"
    assert WriteFileTool(tmp_path).parameters["required"] == ["path", "content"]
    assert EditFileTool(tmp_path).parameters["required"] == ["path", "format", "edit"]
    fmt_schema = EditFileTool(tmp_path).parameters["properties"]["format"]
    assert fmt_schema["enum"] == ["unified_diff", "search_replace", "whole_file"]
    assert tuple(fmt_schema["enum"]) == EDIT_FORMATS


# --- write_file ------------------------------------------------------------------


def test_write_file_creates_parents_and_reports(tmp_path):
    result = run(WriteFileTool(tmp_path), path="a/b/c.txt", content="hello\n")
    assert result.ok
    assert (tmp_path / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "hello\n"
    assert "created" in result.output


def test_write_file_overwrite_then_honest_no_op(tmp_path):
    tool = WriteFileTool(tmp_path)
    assert run(tool, path="f.txt", content="v1\n").ok
    second = run(tool, path="f.txt", content="v2\n")
    assert second.ok
    assert "overwrote" in second.output
    third = run(tool, path="f.txt", content="v2\n")
    assert third.ok
    assert third.data["no_op"] is True
    assert "no changes" in third.output


def test_write_file_writes_exact_bytes_no_newline_translation(tmp_path):
    result = run(WriteFileTool(tmp_path), path="mix.txt", content="a\r\nb\nc")
    assert result.ok
    assert (tmp_path / "mix.txt").read_bytes() == b"a\r\nb\nc"


def test_write_file_leaves_no_temp_files_behind(tmp_path):
    tool = WriteFileTool(tmp_path)
    run(tool, path="f.txt", content="one")
    run(tool, path="f.txt", content="two")
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]


def test_write_file_refuses_jail_escape_and_creates_nothing(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = WriteFileTool(ws)
    for evil in ["../escape.txt", str(tmp_path / "abs-escape.txt")]:
        result = run(tool, path=evil, content="pwned")
        assert not result.ok
        assert result.error
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "abs-escape.txt").exists()
    assert list(ws.iterdir()) == []


def test_write_file_arg_validation_and_directory_target(tmp_path):
    tool = WriteFileTool(tmp_path)
    assert not run(tool, path="f.txt").ok  # content missing
    assert not run(tool, content="x").ok  # path missing
    (tmp_path / "adir").mkdir()
    result = run(tool, path="adir", content="x")
    assert not result.ok
    assert "directory" in result.error


# --- edit_file -------------------------------------------------------------------


def test_edit_file_unified_diff_with_drift_updates_disk(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("pad0\npad1\n" + BASE, encoding="utf-8", newline="\n")
    result = run(
        EditFileTool(tmp_path), path="f.txt", format="unified_diff", edit=REPLACE_LINE10
    )
    assert result.ok, result.error
    text = target.read_text(encoding="utf-8")
    assert "LINE10\n" in text
    assert "line10" not in text


def test_edit_file_search_replace_markers_apply(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("import os\n\ndef main():\n    pass\n", encoding="utf-8", newline="\n")
    edit = d("<<<<<<< SEARCH", "    pass", "=======", "    print('hi')", ">>>>>>> REPLACE")
    result = run(EditFileTool(tmp_path), path="app.py", format="search_replace", edit=edit)
    assert result.ok, result.error
    assert (
        target.read_text(encoding="utf-8") == "import os\n\ndef main():\n    print('hi')\n"
    )


def test_edit_file_whole_file_no_op_reported_and_untouched(tmp_path):
    target = tmp_path / "same.txt"
    target.write_bytes(b"alpha\r\nbeta\r\n")
    result = run(
        EditFileTool(tmp_path), path="same.txt", format="whole_file", edit="alpha\nbeta\n"
    )
    assert result.ok
    assert result.data["no_op"] is True
    assert "no changes" in result.output
    assert target.read_bytes() == b"alpha\r\nbeta\r\n"


FAILING_EDITS = [
    ("unified_diff", d("@@ -1,2 +1,2 @@", " ghost", "-phantom", "+other")),
    ("search_replace", d("<<<<<<< SEARCH", "phantom", "=======", "other", ">>>>>>> REPLACE")),
    ("search_replace", d("<<<<<<< SEARCH", "dup", "=======", "DUP", ">>>>>>> REPLACE")),
    ("whole_file", "x" * (WHOLE_FILE_MAX_CHARS + 1)),
]


@pytest.mark.parametrize(
    "fmt,edit", FAILING_EDITS, ids=["bad-hunk", "no-match", "ambiguous", "oversize"]
)
def test_edit_file_failure_leaves_file_byte_identical(tmp_path, fmt, edit):
    target = tmp_path / "code.py"
    target.write_bytes(b"dup\r\nmid\ndup\n")  # deliberately mixed endings: hardest case
    before = target.read_bytes()
    result = run(EditFileTool(tmp_path), path="code.py", format=fmt, edit=edit)
    assert not result.ok
    assert result.error  # mechanical reason surfaced for the repair loop
    assert target.read_bytes() == before


def test_edit_file_crlf_file_keeps_crlf_bytes_on_disk(tmp_path):
    target = tmp_path / "dos.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    edit = d("<<<<<<< SEARCH", "two", "=======", "TWO", ">>>>>>> REPLACE")
    result = run(EditFileTool(tmp_path), path="dos.txt", format="search_replace", edit=edit)
    assert result.ok, result.error
    assert target.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


def test_edit_file_lf_file_stays_lf_on_disk(tmp_path):
    target = tmp_path / "unix.txt"
    target.write_bytes(b"one\ntwo\n")
    edit = d("<<<<<<< SEARCH", "two", "=======", "TWO", ">>>>>>> REPLACE")
    result = run(EditFileTool(tmp_path), path="unix.txt", format="search_replace", edit=edit)
    assert result.ok, result.error
    assert target.read_bytes() == b"one\nTWO\n"


def test_edit_file_refuses_jail_escape_without_touching_the_file(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "victim.txt"
    outside.write_text("secret\n", encoding="utf-8")
    result = run(
        EditFileTool(ws), path="../victim.txt", format="whole_file", edit="pwned\n"
    )
    assert not result.ok
    assert result.error
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_edit_file_missing_file_points_to_write_file(tmp_path):
    result = run(EditFileTool(tmp_path), path="ghost.py", format="whole_file", edit="x\n")
    assert not result.ok
    assert "not found" in result.error
    assert "write_file" in result.error


def test_edit_file_rejects_unknown_format_listing_the_ladder(tmp_path):
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    result = run(EditFileTool(tmp_path), path="f.txt", format="patch", edit="x")
    assert not result.ok
    for fmt in EDIT_FORMATS:
        assert fmt in result.error


def test_edit_file_refuses_binary_and_non_utf8(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"MZ\x00payload")
    binary = run(EditFileTool(tmp_path), path="blob.bin", format="whole_file", edit="x")
    assert not binary.ok
    assert "binary" in binary.error
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9\n")  # latin-1, invalid utf-8
    latin = run(EditFileTool(tmp_path), path="latin.txt", format="whole_file", edit="x")
    assert not latin.ok
    assert "UTF-8" in latin.error
    assert (tmp_path / "latin.txt").read_bytes() == b"caf\xe9\n"
