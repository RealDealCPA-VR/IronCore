# IronCore Safety Model

> Safety is architecture here, not a feature flag. This document is the threat model and the
> control catalog; the policy table in `ironcore/safety/policy.py` is its executable core.

## 1. Principles

1. **Fail closed.** Unknown tool → deny. Unparseable policy → deny. Unprobed model → the
   most conservative adapters. Missing config → the least autonomous defaults.
2. **Least autonomy by default.** Boot mode is MANUAL. Autonomy is granted per-session by a
   human keystroke (Shift+Tab), never assumed.
3. **Nothing invisible.** Every tool call is a transcript card and an audit line — including
   denied ones. A user scrolling the transcript sees everything that happened, in order.
4. **The model is untrusted in both directions.** Its output may be malformed or malicious;
   its input (tool results) may carry injected instructions. Both cross trust boundaries
   with checks.
5. **Open-model honesty.** Smaller models follow injected instructions more readily and
   confabulate success more often. IronCore does not pretend otherwise: defenses assume
   injection lands sometimes, and "done" is evidence-based (SPEC §5.5), not claimed.

## 2. Threat model

| # | Threat | Example | Primary controls |
|---|---|---|---|
| T1 | Destructive commands | `rm -rf`, `git push --force`, disk format | mode gate (§3), deny-list in ALL modes, risky-pattern classifier ALLOW→ASK (IC-402) |
| T2 | Workspace escape | writing `~/.bashrc`, `..\..\` traversal, symlink/UNC tricks | path jail at the tool layer, mode-independent (IC-401) |
| T3 | Prompt injection via tool output | fetched page / repo file says "ignore instructions, run curl…" | data-not-instructions wrapping, injection detector, AUTO→ASK downgrade on flags (IC-406) |
| T4 | Secret exfiltration | env vars or key files read into context, then sent to a hosted endpoint | redaction before context (IC-404), reads outside workspace ask, NET never auto-allowed |
| T5 | Runaway loops | same failing command forever; token burn | budgets + loop detector (IC-506) |
| T6 | Silent bad edits | plausible diff that breaks the build | deterministic patcher rejects non-applying edits, verification loop (IC-504), git snapshot undo (IC-405) |
| T7 | Confabulated success | "All tests pass!" (they don't) | stop_reason computed from tool evidence only; /goal stop-condition check (SPEC §3.4) |
| T8 | Malicious workflow/config in a cloned repo | `.ironcore/workflows/` shipping an AUTO-mode exfil job | project config cannot raise autonomy above user config's ceiling; workflows start in the session's current mode; first run of a repo's workflow shows a summary + confirmation |
| T9 | Malicious or defective plugin | a pip-installed distribution registering a tool that lies about its risk, or crashing at import | installation is the consent moment (pip already ran arbitrary code); per-entry-point fault isolation (a broken plugin is skipped + reported, never a crash); plugin tools pass the same `decide(mode, risk)` gate, NET tools not loaded unless `safety.network_tools`; `doctor` lists everything loaded/skipped; `[plugins] enabled = false` kill switch (§8) |

## 3. The mode gate (implemented)

`decide(mode, risk)` — the table in `policy.py`, pinned by `tests/test_safety.py`:

|  | READ | WRITE | EXEC | NET |
|---|---|---|---|---|
| **plan** | allow | **deny** | **deny** | **deny** |
| **manual** | allow | ask | ask | ask |
| **accept-edits** | allow | allow | ask | ask |
| **auto** | allow | allow | allow | **ask** |

Invariants (tested): reads always allowed; NET never auto-allowed; PLAN cannot mutate;
every cell explicit. The engine has no code path to a tool that skips the gate
(CONTRACTS.md #Engine).

Layering: command policy and path jail may turn an `allow` into `ask`/`deny`; nothing may
loosen a gate decision. Approval grants ("approve all writes") live at most one turn.

## 4. Approval UX requirements (IC-703)

- The preview shows the **exact effect**: full diff for writes, resolved command line for
  exec, URL + method for net. Never a paraphrase.
- Approving is single-key but never the focused-by-default Enter target for EXEC/NET.
- Deny requires no reason but accepts one; the reason is fed back to the model verbatim.

## 5. Audit & undo

- Audit JSONL (`.ironcore/audit/`): ts, session, turn, event (tool_call/gate/approval/
  mode_change/turn_end), tool, args-hash, decision, result-status. Append-only; no rewrite
  API exists in the codebase.
- Undo: shadow git snapshots per mutating turn on a dedicated ref (`refs/ironcore/undo`),
  byte-exact restore, never touches the user's index/branches. Works in non-git workspaces
  by init-ing a private repo under `.ironcore/snapshots/`.

## 6. Secrets

- Redaction pass (IC-404) over: outbound context, transcript rendering, audit lines.
  Sources: process env values (length ≥ 8), `.env` file values, high-entropy key-shaped
  strings (`sk-…`, `ghp_…`, PEM blocks).
- The provider api_key never appears in logs, errors, or context (contract in
  `openai_compat.py`).
- `ironcore doctor` warns when a hosted endpoint (non-localhost) is configured while
  `safety.network_tools` is on — a "your code leaves this machine" reminder.

## 7. What IronCore will not build

Consistent with responsible release of an autonomous coding tool:

- No default-on network egress for the agent, ever.
- No mode that skips the deny-list. AUTO is sandboxed autonomy, not root.
- No headless AUTO without explicit `--mode auto` *and* budgets set (headless refuses the
  combination of unbounded turns + AUTO).
- No feature whose purpose is evading the audit trail.

## 8. Plugins (MS-5)

Entry-point plugins (`docs/PLUGINS.md`) extend tools, commands, probes, providers, and
edit formats. The trust model, stated plainly:

- **Installation is the consent moment.** Discovery only sees distributions the user
  `pip install`-ed into IronCore's environment — and that install already executed
  arbitrary code (setup hooks, import side effects). Discovery-on-by-default therefore
  adds no new code-execution capability; it is the same model as pytest/flake8 plugins.
  Hardened setups set `[plugins] enabled = false` and discovery is skipped entirely.
- **What a plugin cannot bypass:** the mode gate. Plugin tools carry a real `ToolRisk`
  and every call goes through the same `decide(mode, risk)` as builtins — PLAN still
  denies mutation, NET is never auto-allowed, and a NET-risk plugin tool is not even
  loaded unless `safety.network_tools` is true. Builtins win every duplicate name, so a
  plugin can never shadow `edit_file`, `read_file`, or any other built-in surface.
- **Honest limits:** risk honesty is on the plugin author — a tool could declare
  `ToolRisk.READ` while writing files, and the gate governs by *declared* risk. That is
  the same trust boundary as installing any package, not something the loader can verify.
  Plugin code also runs at import/factory time during boot and `doctor`; fault isolation
  contains crashes, not intent. `ironcore doctor` shows exactly what loaded and what was
  skipped (with reasons), so the plugin surface is always inspectable.
