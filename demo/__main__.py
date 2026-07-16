"""``python -m demo`` — run the offline session, narrated, in a throwaway dir.

Creates a ``tempfile`` workspace so re-runs are clean, drives the demo with
``print`` as the narration sink, and exits 0 on a clean, verified ``done``. The
temp dir is removed on the way out (``ignore_errors`` so a lingering OS handle on
Windows can never turn a successful demo into a nonzero exit).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from demo.scenario import run_demo


def main() -> int:
    workspace = tempfile.mkdtemp(prefix="ironcore-demo-")
    try:
        return run_demo(workspace=Path(workspace), emit=print)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
