"""ShellTool contract: success, exit codes, timeout tree-kill, output caps.

Portable by construction: every command shells out to ``sys.executable -c``
so the same test drives cmd.exe on Windows and /bin/sh on POSIX. The Python
code inside ``-c`` uses only single quotes, so double-quoting the argument
is safe in both shells (and survives spaces in the interpreter path).
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

from ironcore.safety.risk import ToolRisk
from ironcore.tools.shell import (
    DEFAULT_TIMEOUT_S,
    MAX_OUTPUT_CHARS,
    MAX_TIMEOUT_S,
    ShellTool,
    _clamp_timeout,
)


def _py(code: str) -> str:
    """A command line running ``code`` with this interpreter, quoted for both shells."""
    assert '"' not in code, "use single quotes inside -c code"
    return f'"{sys.executable}" -c "{code}"'


def test_contract_surface(tmp_path):
    tool = ShellTool(workspace=tmp_path)
    assert tool.name == "shell"
    assert tool.risk is ToolRisk.EXEC
    assert tool.parameters["required"] == ["command"]
    spec = tool.spec()
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "shell"


def test_success_command_stdout_and_exit_code(tmp_path):
    tool = ShellTool(workspace=tmp_path)
    command = _py("print('iron-ok')")
    result = asyncio.run(tool.run(command=command))
    assert result.ok
    assert result.error is None
    assert result.output.strip() == "iron-ok"
    assert result.data["exit_code"] == 0
    assert result.data["timed_out"] is False
    # The engine's approval preview reads these — exact command, resolved cwd.
    assert result.data["command"] == command
    assert result.data["cwd"] == str(Path(tmp_path).resolve())


def test_nonzero_exit_code_surfaced(tmp_path):
    tool = ShellTool(workspace=tmp_path)
    result = asyncio.run(tool.run(command=_py("import sys; sys.exit(3)")))
    assert not result.ok
    assert result.data["exit_code"] == 3
    assert result.data["timed_out"] is False
    assert "3" in (result.error or "")


def test_stderr_merged_into_output(tmp_path):
    tool = ShellTool(workspace=tmp_path)
    code = "import sys; print('to-stdout'); print('to-stderr', file=sys.stderr)"
    result = asyncio.run(tool.run(command=_py(code)))
    assert result.ok
    assert "to-stdout" in result.output
    assert "to-stderr" in result.output


def test_timeout_kills_process_tree(tmp_path):
    # sleep(5) under a 1s timeout: the tree-kill must close the pipes, so the
    # tool returns in ~1s. If only the shell died and the python grandchild
    # kept the pipe open, this would take the full 5s and fail the bound.
    tool = ShellTool(workspace=tmp_path)
    start = time.monotonic()
    result = asyncio.run(
        tool.run(command=_py("import time; time.sleep(5)"), timeout_s=1.0)
    )
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"tree not killed: took {elapsed:.1f}s"
    assert not result.ok
    assert result.data["timed_out"] is True
    assert result.data["exit_code"] != 0
    assert "timed out after 1s" in result.output
    assert "timed out" in (result.error or "")


def test_large_output_truncated_with_exact_marker(tmp_path):
    tool = ShellTool(workspace=tmp_path)
    extra = 10_000
    n = MAX_OUTPUT_CHARS + extra
    # sys.stdout.write => no newline translation, exactly n chars on both OSes.
    result = asyncio.run(tool.run(command=_py(f"import sys; sys.stdout.write('x'*{n})")))
    assert result.ok
    assert result.output[:MAX_OUTPUT_CHARS] == "x" * MAX_OUTPUT_CHARS
    assert result.output.endswith(f"... [truncated: {extra} more chars]")


def test_small_output_not_truncated(tmp_path):
    tool = ShellTool(workspace=tmp_path)
    result = asyncio.run(tool.run(command=_py("import sys; sys.stdout.write('y'*64)")))
    assert result.ok
    assert result.output == "y" * 64
    assert "[truncated" not in result.output


def test_cwd_is_workspace_relative(tmp_path):
    sub = tmp_path / "subdir"
    sub.mkdir()
    tool = ShellTool(workspace=tmp_path)
    result = asyncio.run(
        tool.run(command=_py("import os; print(os.getcwd())"), cwd="subdir")
    )
    assert result.ok
    assert Path(result.output.strip()).resolve() == sub.resolve()
    assert result.data["cwd"] == str(sub.resolve())


def test_missing_cwd_is_a_tool_error_not_an_exception(tmp_path):
    tool = ShellTool(workspace=tmp_path)
    result = asyncio.run(tool.run(command=_py("print('never runs')"), cwd="no-such-dir"))
    assert not result.ok
    assert "cwd" in (result.error or "")
    assert result.data["exit_code"] == -1


def test_timeout_clamp_defaults_and_caps():
    assert _clamp_timeout(None) == DEFAULT_TIMEOUT_S
    assert _clamp_timeout(5) == 5.0
    assert _clamp_timeout(MAX_TIMEOUT_S + 1000) == MAX_TIMEOUT_S
    with pytest.raises(ValueError):
        _clamp_timeout(0)
    with pytest.raises(ValueError):
        _clamp_timeout(-1)
