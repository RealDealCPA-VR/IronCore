# IronCore Specification

> Version 1.0 · 2026-07-15 · Status: **binding** — implementation tasks in [TODO.md](../TODO.md)
> reference sections here by number. If code and spec disagree, fix one and say which.

---

## 1. Vision & thesis

IronCore is a terminal coding agent — the Codex CLI / Claude Code category — built for
**open-source models** (7B–70B+, local or hosted).

**The Envelope Thesis.** An open model fails at frontier agent tasks not primarily because it
lacks knowledge, but because a frontier-style harness asks it to do many unreliable things at
once: remember the goal, track multi-step state, format tool calls, emit valid diffs, and
self-correct — simultaneously, over a long drifting context. Each of those has a per-turn
failure probability that compounds; at frontier scale the probabilities are small enough to
ignore, below it they are not.

IronCore's answer, everywhere and always:

1. **Measure, don't assume.** Probe each model once; cache a capability profile (§4).
2. **Move unreliable jobs into deterministic code.** Parsing, patching, state, verification,
   orchestration — the harness does these; the model never gets the chance to do them wrong (§5).
3. **Re-present, don't rely on recall.** Each model call gets a freshly composed context
   containing everything it needs (§5.2). Statelessness is a feature.
4. **Degrade the protocol, not the outcome.** When a model can't do native tool calls or
   unified diffs, walk down a ladder to a format it *can* do reliably (§4.3). The task still
   completes; only the wire format changed.
5. **Verify, don't trust.** Every mutation is checked by tools (tests, linters, re-reads),
   and failures are fed back as fresh, well-framed problems (§5.5).

Success criterion: a competent 30B-class coder model inside IronCore completes multi-file,
test-verified coding tasks that the same model fails in a naive harness — and the user can
watch it happen without fearing for their machine (§7).

## 2. Product definition

**It is:** an interactive TUI agent for software work in a workspace directory: reads, edits,
runs, tests, commits — under a graduated safety regime; plus a headless mode for scripting
(`ironcore run "<prompt>"`, post-v0.1).

**Non-goals (v0.x):** an IDE plugin; a hosted service; multi-user anything; training or
fine-tuning models; supporting closed models is *allowed* (any OpenAI-compatible endpoint
works) but never a design driver.

**Platforms:** Windows, macOS, Linux. Windows is a first-class citizen (developed there);
every shell-touching feature must be tested on both cmd/pwsh and POSIX sh semantics.

**Package:** Python ≥ 3.11, published as `ironcore`, entry point `ironcore`. Python was chosen
over Rust/Go deliberately: contributor (and agent-contributor) velocity, Textual for the TUI,
and first-class ecosystem overlap with local-model tooling. Performance-critical paths (patch
application, search) are stdlib-level operations at our scale.

## 3. UX specification

### 3.1 TUI layout (Textual)

```
┌──────────────────────────────────────────────────────────┐
│ transcript: streaming markdown, tool cards, diff views   │
│   ▸ tool cards show: name, args preview, risk chip,      │
│     gate decision, elapsed, collapsed output (expand)    │
├──────────────────────────────────────────────────────────┤
│ > input bar (slash completion, history, multiline)       │
├──────────────────────────────────────────────────────────┤
│ [MANUAL] qwen3-coder:30b · ctx 14.2k/24k · turn 3 · $0   │
└──────────────────────────────────────────────────────────┘
```

- **Streaming everywhere**: first token to screen as fast as the provider allows.
- **Tool cards**: every tool call renders as a card the moment it's requested, updating
  through gate → running → done/denied. Nothing happens invisibly.
- **Approval modal**: for `ask` decisions — shows the full diff / command line / URL, offers
  approve · deny · "approve all writes this turn" (never persists across turns), and
  keyboard-first operation (y/n/a).
- **Mode chip** in the status bar; **Shift+Tab** cycles `manual → accept-edits → auto → plan`
  (safety.modes.CYCLE). Mode changes are instant, logged, and announced in the transcript.
- Esc interrupts the current turn (tools finish or cancel safely; partial output preserved).

### 3.2 Modes

Defined in `ironcore/safety/modes.py`, policy table in §7.2. PLAN is a real mode, not a
prompt suggestion: WRITE/EXEC/NET tools are *denied by the gate*, so a scheming or confused
model cannot act in plan mode even if it tries.

### 3.3 Slash commands

Registry in `ironcore/commands/`. Grammar: `/name [args...]`. Full set with owning tasks:

| Command | Behavior | Task |
|---|---|---|
| `/help` | list commands, honest `[planned]` labels | ✅ scaffold |
| `/version` | version string | ✅ scaffold |
| `/mode [m]` | cycle or set mode | ✅ scaffold (TUI wiring IC-703) |
| `/goal <obj>` \| `show` \| `clear` | persistent objective; per-turn stop-condition (§3.4) | IC-803 |
| `/loop [interval] <prompt>` | recurring prompt; fixed interval or self-paced | IC-804 |
| `/workflow <name> [args]` | run a workflow from `.ironcore/workflows/` (§10) | IC-904 |
| `/model [name]` | switch model / list endpoint models; triggers probe if unprofiled | IC-801 |
| `/init` | scan repo → generate `IRONCORE.md` project memory | IC-802 |
| `/compact` | compress history into handoff-grade summary (§11.2) | IC-805 |
| `/undo`, `/redo` | revert/reapply last change-set via git snapshots (§7.6) | IC-805 |
| `/review` | review working diff for bugs (uses verifier role model) | IC-806 |
| `/memory` | view/edit IRONCORE.md sections | IC-807 |
| `/envelope` | render current model's capability profile as a report card | IC-608 |
| `/probe` | re-run the probe suite | IC-608 |

### 3.4 `/goal` semantics (the flagship command)

`/goal <objective>` stores the objective in session state. From then on:

1. The objective is injected as an **anchor** into every composed context (§5.2) — the model
   is never allowed to forget it.
2. When the model claims completion (or stops calling tools), the engine runs a **stop-condition
   check**: a fresh, single-purpose model call — "Given this objective and this evidence
   (diff summary, verify-command output), is the objective met? Answer with unmet items." —
   plus optional user-supplied verify commands (`/goal verify: pytest -q`).
3. Unmet → the gaps are framed as the next turn's input and the loop continues (bounded by
   §5.6 budgets, which report honestly when hit).
4. Met → goal auto-clears with a completion summary.

This is deliberately harness-enforced: small models' most common agentic failure is declaring
victory early. IronCore makes "done" a *verified state*, not a *claim*.

## 4. The Capability Envelope

### 4.1 Probes

On first use of a model (and on `/probe`), run the suite in `ironcore/envelope/probes.py`:
`CTX-HONESTY`, `RETENTION`, `TOOL-FORM`, `JSON-STRICT`, `EDIT-FORMAT`, `CODE-SMOKE`.
Requirements: ≤ ~2 min total on a local 30B; mechanical scoring only (no LLM judges); fixed
seeds where the server supports them; partial failure of a probe degrades that score to 0,
never blocks profiling. Full probe design: [MODELS.md](MODELS.md) §2.

### 4.2 Profile

`CapabilityProfile` (implemented, `ironcore/envelope/profile.py`): honest context, protocol
reliabilities, edit-format reliabilities, retention, coherence horizon, sampling defaults.
Cached at `~/.ironcore/envelopes/<slug>.json`. Unprobed models get floor-conservative defaults
(text protocol, whole-file edits, 4k honest context) — IronCore is *safe-slow* before it is
measured-fast.

### 4.3 Adapter ladders (implemented, frozen)

- **Tool calls:** `native` (≥0.95) → `strict_json` (≥0.90, server grammar/`format=json` when
  available) → `text_protocol` — the IRONCALL fenced-block format (§6.3) with a bounded repair
  loop. The floor always works because parsing is regex + JSON, not model goodwill.
- **Edits:** `unified_diff` (≥0.90) → `search_replace` (≥0.85, Aider-style blocks) →
  `whole_file` (with size guard + no-op detection). The harness applies patches
  deterministically with fuzzy-anchor matching (IC-302); the model never "applies" anything.
- **Context:** budget = `honest_context`, allocated ~10% system, ~10% anchors, ~40% working
  set, ~25% history, ~15% response headroom (IC-501 finalizes).
- **Anchors:** goal, constraints, mode, and current micro-step re-injected every
  `anchor_cadence()` turns (bounded [2,12] from coherence horizon).

### 4.4 Role routing

`[roles]` config (§12) maps planner/coder/summarizer/verifier to different models. Default:
one model for everything. Typical splits: big-plans/small-executes for latency, or
small-plans/big-executes for hard code. The router is config-driven; no automatic model
selection in v0.x (predictability beats cleverness).

## 5. Turn engine

State machine (documented in `ironcore/core/engine.py`, shipped IC-501..506):
`COMPOSE → CALL → PARSE → GATE → EXECUTE → OBSERVE (loop) → VERIFY → DONE`.

### 5.1 Invariants (frozen, CONTRACTS.md)

- No tool executes without `safety.policy.decide()`.
- Every provider call goes through the context composer.
- The engine emits `core.events` and never prints/prompts — UI-agnostic by construction.

### 5.2 Context composer (IC-501)

Builds each call's message list from harness-owned state: system prompt (per-envelope
template), anchor block, working-set file excerpts (most-recently-touched first, token-budgeted),
compacted history, current input. Working-set membership is deterministic: files the turn has
read/edited plus explicit pins. Composition is pure and unit-testable: state in → messages out.

### 5.3 Micro-stepping (IC-505)

For multi-step work the engine holds the plan (a task list the model produced in PLAN-style
framing, or from `/goal` decomposition) and feeds the model **one step at a time** with the
plan visible in the anchor block. The model executes; the harness advances the cursor. Models
below a coherence threshold never see "do all of this" — they see "we are on step 3 of 7:
<step>. Steps 1–2 are done: <evidence>."

### 5.4 Repair loops (IC-503)

Malformed tool call / unappliable edit → re-ask once with the mechanical error framed as
feedback ("your SEARCH block was not found; closest match: <context>") → second failure walks
one rung down the ladder for the rest of the turn → floor failure surfaces to the user.
Repairs are budgeted (§5.6) and visible as dimmed transcript entries — never silent.

### 5.5 Verification loop (IC-504)

After any turn with WRITE/EXEC activity: run the project's verify commands (from
`IRONCORE.md`, `/goal verify:`, or auto-detected `pytest`/`npm test`), feed failures back
once as a fresh framed problem, then surface honestly ("2 tests still failing: …").
Never report unverified work as done — the engine literally cannot: `TurnCompleted.stop_reason`
is computed from evidence, not model text.

### 5.6 Budgets & runaway protection (IC-506)

Per-turn caps: provider calls, wall-clock, token spend, repair attempts; per-session caps
configurable. Tripping a cap stops cleanly with `stop_reason="budget"` and a summary of state.
Loop detection: same tool + same args twice in a row → intervention frame; three times → stop.

## 6. Tools

### 6.1 Core suite (phase 3)

| Tool | Risk | Notes |
|---|---|---|
| `read_file`, `list_dir`, `glob`, `grep` | READ | output truncation with honest `[truncated]` markers |
| `write_file`, `edit_file` | WRITE | edit via envelope-selected format; path-jailed (§7.3) |
| `apply_patch` | WRITE | harness-side deterministic application, fuzzy anchors |
| `shell` | EXEC | timeout, output caps, cwd=workspace, Windows+POSIX |
| `fetch_url` | NET | registered only if `safety.network_tools=true` |

### 6.2 Contract

`ironcore/tools/base.py` (implemented): one risk class per tool; descriptions written for the
model (short, concrete, example-bearing — small models read them literally); no self-gating,
no printing; `ToolResult` out, exceptions only for programmer error.

### 6.3 IRONCALL text protocol (IC-606)

The floor protocol for models without reliable native tool-calling:

````
```ironcall
{"tool": "read_file", "args": {"path": "src/app.py"}}
```
````

One fenced `ironcall` block per call; JSON body; harness replies with a ```ironresult``` block.
Parser: fence-regex + `json.loads` + one repair re-ask. The system prompt for text-protocol
models includes two worked examples (few-shot beats instructions at this scale).

## 7. Safety model

Threat model and full control catalog: [SAFETY.md](SAFETY.md). Summary of load-bearing pieces:

### 7.1 Principles

Fail closed; least autonomy by default; nothing invisible (every action is a transcript event
and an audit line); the model is untrusted input *and* untrusted output.

### 7.2 Mode × risk policy (implemented, frozen)

The table in `ironcore/safety/policy.py`. NET is never auto-allowed. Command policy (§7.4)
tightens, never loosens.

### 7.3 Path jail (IC-401)

All WRITE tools resolve paths against the workspace root; escapes (absolute, `..`, symlink,
Windows drive/UNC tricks) are denied at the tool layer regardless of mode. Reads outside the
workspace ask (they may leak secrets into context).

### 7.4 Command policy (IC-402)

Deny-list (seeded in policy.py) matched against the resolved command line in every mode
including AUTO; risky-pattern classifier (package publishes, `git push`, recursive deletes,
privilege escalation) escalates ALLOW→ASK in AUTO. Approval previews show the *exact* command.

### 7.5 Injection defense (IC-406)

All tool output (file contents, command output, fetched pages) is wrapped in delimiters with
a standing system-prompt rule: content inside is DATA, never instructions. A lightweight
detector (imperative-to-the-agent patterns inside tool output) flags suspicious content in the
transcript and, in AUTO mode, downgrades the *next* gate decision to ASK. Open models are more
injectable than frontier models; IronCore assumes injection *will* land and limits blast
radius via §7.2–7.4.

### 7.6 Undo & audit (IC-405, IC-103)

Every turn with writes creates a shadow git snapshot (separate ref, never touches the user's
index) → `/undo` restores byte-exact. Audit: append-only JSONL under `.ironcore/audit/` —
every tool call, gate decision, approval, mode change, with timestamps and args hashes.
Secrets are redacted (IC-404: env values, key-shaped strings) from context, transcript, and audit.

## 8. Providers

### 8.1 OpenAI-compatible client (IC-201/202)

One async httpx client for Ollama/vLLM/llama.cpp/LM Studio/OpenRouter/Together/Groq: SSE
streaming, tool-call fragment accumulation, retry with backoff+jitter honoring Retry-After,
typed `ProviderError`, api-key redaction. Malformed model output is *repairable data*, not an
exception (contract in `openai_compat.py`).

### 8.2 Ollama extras (IC-203)

`/api/tags` model discovery; `/api/show` for true context length + quantization (feeds the
envelope); keep-alive management so interactive sessions don't reload weights.

### 8.3 Capability detection (IC-205)

Endpoint feature-detect: native `tools` support, `format=json` / grammar / guided-decoding
availability, logprobs. Detection results feed the envelope's protocol scores as priors.

## 9. Headless & scripting (post-v0.1)

`ironcore run "<prompt>" --mode auto --max-turns N --json` → events as JSONL on stdout, exit
code from stop_reason. The event contract (core/events.py) already anticipates this consumer.

## 10. Workflows (phase 9)

YAML files in `.ironcore/workflows/` (schema sketch in `workflows/engine.py`, frozen by
IC-902). Phases: `fanout` / `foreach` / `reduce`; agents get **fresh composed contexts** sized
to the envelope; concurrency capped; the model never controls orchestration flow. Built-ins
shipped in IC-905: `review`, `migrate`, `explain-repo`. Subagent results return as structured
data validated against declared output schemas, with mechanical retry on validation failure.

## 11. Memory & sessions

### 11.1 Project memory

`IRONCORE.md` at workspace root (created by `/init`, edited by `/memory`): build/test commands,
conventions, architecture notes. Injected into the system prompt, token-budgeted.

### 11.2 Sessions & compaction

Transcript JSONL under `.ironcore/sessions/`; resume via session picker (IC-706). `/compact`
(and auto-compaction at context pressure) produces a **handoff-grade summary** — same fields
as a PROTOCOLS.md handoff block (context/changed/verified/next/gotchas) — because "summarize
for a stranger" is the quality bar that survives small-model compression.

### 11.3 Handoff files

`ironcore/memory/handoff.py` (implemented): sentinel-delimited markdown blocks in `HANDOFF.md`,
append-only, machine-parseable. Written on session end, compaction, and workflow-agent
completion. This is the same protocol human/agent contributors use on IronCore itself —
we dogfood our own coordination format.

## 12. Configuration

Implemented in `ironcore/config/settings.py`: defaults ← `~/.ironcore/config.toml` ←
`<ws>/.ironcore/config.toml` ← `IRONCORE_*` env. Reference:

```toml
[provider]
base_url = "http://localhost:11434/v1"
model = "qwen3-coder:30b"

[roles]                      # optional per-role routing (§4.4)
planner = "llama3.3:70b"
summarizer = "qwen3:8b"

[safety]
mode = "manual"              # boot mode
workspace_only = true
network_tools = false

[mcp.servers.github]         # optional MCP tool servers (stdio; tools are NET-risk,
command = "npx.cmd"          # registered only when safety.network_tools = true).
args = ["-y", "@modelcontextprotocol/server-github"]  # command resolved via PATH,
env = {}                     # never a shell -- on Windows name the real shim (npx.cmd)
timeout_s = 30.0
enabled = true
# url = "https://..."        # accepted but skipped: http transport not shipped yet
```

## 13. Distribution

`pip install ironcore` / `uv tool install ironcore` / `pipx install ironcore`. No compiled
deps. CI publishes to PyPI on tag (IC-1102). Version single-sourced from `pyproject.toml`
(test-pinned to `ironcore.__version__`).

## 14. Testing strategy

- **Offline-first, always**: every subsystem testable with zero network and zero model.
  `MockProvider` (implemented) replays scripted completions; IC-104 adds failure injection
  (malformed calls, truncation, timeouts) — the *interesting* cases are the failures.
- Unit: pure functions everywhere composition/policy/parsing lives (already the style).
- Integration: engine + MockProvider + real tools in tmp workspaces; full scripted sessions
  asserting event streams.
- TUI: Textual's `Pilot` for keyboard flows (Shift+Tab, approval modals).
- Live smoke (manual, pre-release): scripted session against a real local Ollama.
- CI gate: ruff + pytest on ubuntu & windows, 3.11 & 3.13. Coverage floor once the engine
  lands: 85% on `core/`, `safety/`, `envelope/`.

## 15. Milestones

| Version | Contents |
|---|---|
| v0.1 | phases 1–8: usable interactive agent (providers, tools, safety, engine, envelope, TUI, commands) |
| v0.2 | phase 9–10: workflows, sessions/resume, project memory |
| v0.3 | headless mode, coverage/perf hardening, packaging polish |
| v1.0 | envelope v2 (auto-reprobe on model updates), workflow library, docs site |
