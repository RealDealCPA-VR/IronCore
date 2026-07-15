# AGENTS.md — instructions for AI agents working in this repo

You are contributing to **IronCore**, a terminal coding agent for open-source models.
This repo is built by the same protocol it implements: state lives in files, tasks are
one-pass sized, and verification is evidence, not claims.

## Before writing any code (the pickup ritual)

1. Read the newest handoff block(s) in `HANDOFF.md` (if the file exists).
2. Find your task in `TODO.md`. Confirm its `Depends` are `[x]` and its `Files` aren't
   claimed by an in-flight `[~]` task.
3. Verify the green baseline yourself — if it's red, STOP and report; do not build on red:
   ```
   uv run --extra dev pytest -q
   uv run --extra dev ruff check .
   ```
4. Read the task's Spec references (`docs/SPEC.md` sections are binding) and
   `docs/CONTRACTS.md` for any interface you touch.
5. Claim the task: `[ ]` → `[~] (your-id, YYYY-MM-DD)`.

## While working

- Touch **only** the task's `Files` list. Needing other files means the task is mis-scoped —
  fix the ledger first or stop.
- `docs/CONTRACTS.md` interfaces are **frozen**. If your change needs a contract change,
  the CONTRACTS.md edit goes in the same commit, with a migration note.
- Match existing style: module docstrings state each package's rules; comments explain
  constraints, not narration. Line length 100. `ruff check .` must stay clean.
- Everything must work offline: no test may need a network or a real model. Use
  `MockProvider` (`ironcore/providers/mock.py`).
- Windows is first-class: no POSIX-only paths, no `/tmp`, mind CRLF in fixtures.
- Dependency rules (`docs/ARCHITECTURE.md` §4): `safety/` imports stdlib only; nothing
  imports `tui/`; providers never import tools/core/commands.

## Before you stop

1. Run the task's **Verify** commands plus the full baseline; all green.
2. Flip the task to `[?]` (or `[x]` if Verify is fully mechanical) with evidence.
3. Append a handoff block to `HANDOFF.md` (format: `docs/PROTOCOLS.md` §2 — the
   sentinels matter, `ironcore/memory/handoff.py` parses them).
4. If your work completes a phase (or changes what "works today"), update README.md —
   the roadmap table and the Quickstart status note — in the same change set. The README
   must never claim less or more than what is actually shipped.
5. Report: files changed, verify output, gotchas. Do **not** push unless explicitly asked.

## Never

- Refactor neighbors, upgrade dependencies, or edit other tasks "while you're in there".
- Report unverified work as done — say "not verified" and why instead.
- Commit secrets, absolute local paths, or personal identifiers.
