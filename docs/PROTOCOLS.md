# Hand-off / Pick-up Protocols

> How work moves between contributors — human or agent, same session or months apart —
> without anything living in anyone's head. These protocols are dogfood: IronCore itself
> implements the same handoff format (`ironcore/memory/handoff.py`) for its own sessions.

## 0. The one rule

**The repo is the memory.** If you learned it, decided it, or broke it, it goes in a file
(`TODO.md`, `HANDOFF.md`, `docs/CONTRACTS.md`) before you stop. An undocumented decision
is a decision that will be re-litigated, wrongly, by the next contributor.

## 1. The task ledger (`TODO.md`)

Every unit of work is a ledger task with a stable ID (`IC-xxx`). States:

| Marker | State | Meaning |
|---|---|---|
| `[ ]` | open | unclaimed; anyone may take it if its `Depends` are `[x]` |
| `[~]` | claimed | someone is on it — marker carries `(owner, YYYY-MM-DD)` |
| `[?]` | review | built + self-verified; wants a second pair of eyes |
| `[x]` | done | acceptance criteria verified; evidence recorded in the handoff |

Rules:

1. **Claim before you code.** Flip `[ ]`→`[~]` with your name/agent-id and date, commit that
   (or include it in your first commit). Two claims on one task = the earlier timestamp wins.
2. **A stale claim (>3 days, no commits touching the task's files) is reclaimable.** Note the
   takeover in your handoff.
3. **Never start a task whose dependencies aren't `[x]`.** If you think a dependency is
   wrong, fix the ledger first, in its own commit, with a sentence of justification.
4. **Scope is the task's `Files` list.** Needing to touch other files is a signal: either the
   task is mis-scoped (fix the ledger) or you're about to collide with someone (check claims).
   Shared interfaces you must not drift: [CONTRACTS.md](CONTRACTS.md).
5. Tasks are sized for **one pass** (§4). If you can't finish, un-claim (`[~]`→`[ ]`), write a
   handoff explaining exactly where you stopped, and leave the tree green.

## 2. Handoff blocks (`HANDOFF.md`)

Append a block when you: finish a task, un-claim a task, end a session mid-work, or make a
decision that isn't visible in code. Format (sentinels required — parsed by
`ironcore/memory/handoff.py`):

```markdown
<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-15T18:30:00+00:00 — <name or agent-id>
**Context:** IC-201 OpenAI-compat provider; picking up from scaffold stub.
**Changed:** ironcore/providers/openai_compat.py (full impl), tests/providers/ (12 tests).
**Verified:** `uv run --extra dev pytest -q` → 57 passed. `ruff check .` clean.
**Next:** IC-202 tool-call parsing; the SSE accumulator in _merge_fragments is where it hooks in.
**Gotchas:** Ollama sends `finish_reason` on a SEPARATE final chunk from usage — don't merge them.
<!-- HANDOFF v1 END -->
```

Field discipline:

- **Verified** is commands + observed output, or the literal words "not verified" with why.
  Claiming verification that didn't happen is the one unforgivable protocol violation.
- **Next** leads with the single most useful next action, not a wish list.
- **Gotchas** is for the thing that cost you 20 minutes and will cost the next person 20 more.

## 3. Pickup ritual

Run this **before writing any code**, every time:

```
1. Read the newest HANDOFF.md block(s) since the last one you know.
2. Read TODO.md; find your task; check its Depends are [x] and its Files are unclaimed.
3. Verify the green baseline yourself:
       uv run --extra dev pytest -q      → must pass
       uv run --extra dev ruff check .   → must be clean
   If the baseline is red: STOP. Fixing it (or reporting it) is now your task.
4. Read the task's Spec references (SPEC.md sections) and CONTRACTS.md entries for any
   interface you'll touch.
5. Claim the task ([ ] → [~] with owner+date).
```

## 4. One-pass task rules (for authors of new tasks)

A ledger task must be completable by a single competent agent in a single context window:

- **Single owner, single concern** — one subsystem, one behavior.
- **Explicit files** — everything created/modified is listed; no "and related files".
- **Runnable acceptance** — criteria phrased so `pytest`/`ruff`/a command can adjudicate.
  "Works well" is not acceptance; "streams tool-call fragments across chunk boundaries
  (test: test_fragmented_call)" is.
- **Dependencies are IDs**, not prose.
- **No hidden research** — if the approach needs deciding, that's a separate design task
  whose deliverable is a spec section.
- Rule of thumb: description ≤ 10 lines, diff ≤ ~500 lines, wall-clock ≤ ~90 min.

## 5. Subagent briefing template

When orchestrating (Claude Code, IronCore workflows, or any fan-out), brief each agent with:

```
ROLE: implement exactly one ledger task in the IronCore repo.
TASK: <paste the full TODO.md entry — ID, Files, Spec refs, Build, Accept, Verify>
PROTOCOL:
  - Run the pickup ritual in docs/PROTOCOLS.md §3 first (baseline must be green).
  - Touch ONLY the task's Files. CONTRACTS.md interfaces are frozen.
  - Match the existing code style; module docstrings explain each package's rules.
  - Self-verify with the task's Verify commands; all tests must pass, ruff clean.
  - Flip the task to [?] (or [x] if Verify is fully mechanical), append a HANDOFF.md
    block per §2, and report: files changed, verify output, gotchas.
DO NOT: refactor neighbors, upgrade dependencies, edit other tasks, or push.
```

Parallel-safety: two agents may run concurrently **only if** their tasks share no `Files` and
no CONTRACTS.md interfaces. The orchestrator checks this before fan-out, not after.

## 6. Commit & merge discipline

- Conventional prefix + task ID: `feat(providers): IC-201 OpenAI-compat streaming client`.
- One task per commit (or a small stack); ledger flip + handoff ride in the final commit.
- The tree is green at every commit on `main`. No exceptions; bisectability is a feature.
- Contract changes: CONTRACTS.md edit **in the same commit** as the interface change, with
  a migration note for in-flight tasks.

## 7. Session-end checklist (humans and agents alike)

```
[ ] tests green, ruff clean (or the redness is documented as the handoff's Context)
[ ] TODO.md reflects reality (claims, states, evidence)
[ ] HANDOFF.md block appended
[ ] user-facing docs updated if anything shipped -- README.md (feature prose, command
    table, Moonshots), CHANGELOG.md, docs/CONFIG.md for any config key or IRONCORE_* env
    var, docs/SPEC.md where the spec now disagrees with the code. They must match reality.
[ ] no secrets, no absolute local paths, no personal identifiers in anything committed
```
