"""``python -m ironcore.demo`` — the offline session, narrated, in a throwaway dir.

The user-facing spelling is ``ironcore demo`` (``ironcore.cli.cmd_demo``); this
module is the equivalent ``python -m`` form and delegates to the same
``run_demo``.

Creates a ``tempfile`` workspace so re-runs are clean, narrates to the shared
terminal console (``ironcore.term`` — styled on a TTY, plain text into a pipe),
and exits 0 on a clean, verified ``done``. The temp dir is removed on the way
out (``ignore_errors`` so a lingering OS handle on Windows can never turn a
successful demo into a nonzero exit).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ironcore.demo.scenario import run_demo


def main() -> int:
    workspace = tempfile.mkdtemp(prefix="ironcore-demo-")
    try:
        return run_demo(workspace=Path(workspace))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
