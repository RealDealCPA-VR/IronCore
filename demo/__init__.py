"""IronCore offline end-to-end demo (IC-1103, SPEC §14).

A self-contained, fully offline demonstration of a realistic IronCore session:
NO network, NO real model. A scripted :class:`~ironcore.providers.mock.MockProvider`
drives the REAL :class:`~ironcore.core.engine.TurnEngine` through a small feature
workflow in a throwaway workspace — read a file, plan, edit it, gate + apply the
edit, run a verification command, and stop on evidence.

Rules for this package:

* It only *consumes* ``ironcore`` (engine, tools, safety, providers) — it never
  reaches into private state or fakes an outcome. Every beat the narration shows
  is a real ``core.events`` event or the actual ``VerifyResult`` the engine used.
* Nothing here imports ``ironcore.tui`` (docs/ARCHITECTURE.md §4): the demo is a
  headless consumer of the same event stream the TUI renders.
* All work happens in a caller-supplied (or throwaway ``tempfile``) workspace, so
  ``python -m demo`` is idempotent and leaves nothing behind.

Entry points:

* ``python -m demo``                 — run it, narrated, in a temp dir (exit 0 on success).
* :func:`demo.scenario.run_demo`     — drive it with an injectable ``emit`` sink
  (the test captures output without touching stdout).
"""

from __future__ import annotations

from demo.scenario import run_demo

__all__ = ["run_demo"]
