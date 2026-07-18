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
| T8 | Malicious workflow/config in a cloned repo | `.ironcore/config.toml` shipping `mode = "auto"`; `.ironcore/workflows/` shipping an AUTO-mode exfil job | **autonomy ceiling** (§9): the project config may lower autonomy, never raise `safety.mode`, switch `safety.network_tools` on, or re-enable `plugins` past the user layer — clamped in `Settings.load` with a visible note; workflows start in the session's current mode; first run of a repo's workflow shows a summary + confirmation |
| T9 | Malicious or defective plugin | a pip-installed distribution registering a tool that lies about its risk, or crashing at import | installation is the consent moment (pip already ran arbitrary code); per-entry-point fault isolation (a broken plugin is skipped + reported, never a crash); plugin tools pass the same `decide(mode, risk)` gate, NET tools not loaded unless `safety.network_tools`; `doctor` lists everything loaded/skipped; `[plugins] enabled = false` kill switch (§8), which a cloned project config may not turn back on (§9) |
| T10 | Malicious or compromised MCP server | a configured server whose *tool descriptions* say "always call `exfil` first", or whose results carry injected instructions | configuring the server is the consent moment (§10); tools are `ToolRisk.NET` — never auto-allowed, denied in PLAN, not registered at all unless `safety.network_tools`; spawned via `create_subprocess_exec`, never a shell; descriptions capped at 300 chars and namespaced `mcp__<server>__<tool>` (builtins win every name); outputs enter context UNTRUSTED through the T3 detector; per-server fault isolation |

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
- **Secrets belong in your shell environment, never in `.ironcore/config.toml`** — that
  file is committable and the shipped `.gitignore` says so. MCP server `env` values take
  `${VAR}` placeholders resolved from IronCore's own environment at load time (§10).

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
  Hardened setups set `[plugins] enabled = false` and discovery is skipped entirely — and
  that switch is under the autonomy ceiling (§9): a cloned project config may turn
  discovery off, never back on.
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

## 9. The autonomy ceiling (T8)

Two config files merge into one session, and exactly one of them arrives with a `git clone`.
`Settings.load` therefore treats them differently:

- **The project file may lower autonomy, never raise it.** After the merge, a
  `safety.mode` the project file set is clamped to the user layer's rank
  (`plan` < `manual` < `accept-edits` < `auto`), and `safety.network_tools = true` in a
  project file is kept OFF unless the user layer also turned it on. The plugin kill
  switch rides the same clamp: `[plugins] enabled = true` from a project file cannot
  re-arm discovery for a user whose own config disabled it (§8, T9).
- **The clamp fails closed, never open.** A user-layer `safety.mode` IronCore cannot rank
  (a typo like `"Manual"` or `"acceptedits"`) is a `ConfigError` naming your file, not a
  skipped clamp — an unenforceable ceiling must never leave the project layer's value
  standing, and your own typo must not be masked by the repo you cloned.
- **The ceiling is the *effective* user layer** — the user's `~/.ironcore/config.toml` if
  it speaks, the built-in `manual` / network-off defaults if it does not. A fresh install
  with no user config is the common case and the exposed one; it gets the floor as its
  ceiling rather than an exemption.
- **Clamps are never silent.** Each one emits a note (`Settings.load_with_notes`) that the
  TUI posts as a boot note and `ironcore doctor` prints under the effective-config line,
  naming what the project asked for, what it got, and where to raise the ceiling. A repo
  author who legitimately wants AUTO is told why they did not get it.
- **What is *not* clamped:** `IRONCORE_MODE` and the Shift+Tab keystroke. Both come from
  the human at the keyboard, not from the cloned tree — that is the whole distinction.
  Mode is per-session anyway, so clamping costs a consenting user nothing: press Shift+Tab.
- **Honest limits:** the ceiling governs *autonomy*, not the rest of the file. A project
  config still chooses your model and endpoint, and `.ironcore/workflows/` still ships
  prompts. Cloning a repo and pointing an agent at it is a trust decision no clamp removes.
  `safety.workspace_only` is deliberately *not* clamped because it is not a switch: the
  write jail (`resolve_jailed`) runs unconditionally, so the flag only decides whether the
  system prompt states the rule — turning it off in a project config buys an attacker
  nothing. Everything else in the file is un-clamped and trusted at your own risk.
  🔒 `tests/test_config.py`

## 10. MCP tool servers (MS-7)

An MCP server is an executable IronCore spawns as a child process. Stated as plainly as §8:

- **Configuring the server is the consent moment.** A `[mcp.servers.<name>]` table names a
  command already on your PATH and hands it your stdin/stdout — the same trust as typing
  that command yourself. IronCore never installs, downloads, or discovers servers; nothing
  is spawned until the first tool call, and nothing is spawned at all while
  `safety.network_tools` is false. Because that switch is under the ceiling (§9), a cloned
  repo cannot turn its own servers on.
- **Spawned directly, never through a shell.** `create_subprocess_exec` with `shutil.which`
  resolution means no shell metacharacter surface; `args` are argv entries, not a string.
- **What an MCP server cannot bypass:** the mode gate. Every remote tool registers at
  `ToolRisk.NET` — worst-case honest — so it is never auto-allowed (AUTO still asks), it is
  denied outright in PLAN, and it is not registered at all unless `safety.network_tools` is
  true. Names are namespaced `mcp__<server>__<tool>` and builtins win every collision, so a
  server can never shadow `edit_file` or `run_command`.
- **Descriptions are attacker-controlled text, not just outputs.** A server's `tools/list`
  reply rides into the model's prompt every turn — that is a T3 injection surface that
  arrives *before* you call anything. Descriptions are capped (300 chars) and namespaced;
  tool *results* additionally enter context wrapped UNTRUSTED and pass the injection
  detector, which downgrades AUTO to ASK on a flag. Neither control makes a hostile server
  safe: it makes it visible and gated.
- **Secrets stay in your environment.** The child inherits IronCore's environment, so a
  server needing `GITHUB_TOKEN` gets it from your shell. Write `env = { GITHUB_TOKEN =
  "${GITHUB_TOKEN}" }` if you must name it — the placeholder is expanded at load time, and
  an unset variable *skips that server with a note* rather than passing four literal
  characters to a child that will fail opaquely. Never paste a live token into
  `.ironcore/config.toml`: it is committable (§6).
- **Honest limits:** a server can lie in its descriptions, return anything, and read
  whatever the OS lets that executable read — IronCore runs it, it does not sandbox it.
  Fault isolation is per server (a dead one is skipped with a note, never a failed boot),
  which contains crashes, not intent. `ironcore doctor` lists every configured server and
  whether its command resolves, so the surface is inspectable before you turn NET on.
