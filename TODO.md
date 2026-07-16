# IronCore Build Plan — the task ledger

> Protocol: [docs/PROTOCOLS.md](docs/PROTOCOLS.md) — claim (`[~]` + owner + date) before you
> code; verify before you flip to `[x]`; leave a HANDOFF.md block. Every task is sized for
> **one pass** by one agent. Spec references are binding.
>
> Baseline check (must be green before AND after your task):
> `uv run --extra dev pytest -q` · `uv run --extra dev ruff check .`

Legend: `[ ]` open · `[~]` claimed · `[?]` needs review · `[x]` done

---

## Phase 0 — Scaffold ✅ (shipped 2026-07-15)

- [x] **IC-001 · Repo scaffold** — package layout, safety kernel (modes/risk/policy + tests),
  envelope profile + ladders (+ tests), tool/provider/command contracts, MockProvider,
  handoff protocol (+ tests), config loader (+ tests), CLI (`--version`, `doctor`), CI, docs
  (SPEC/ARCHITECTURE/PROTOCOLS/SAFETY/MODELS/CONTRACTS), README.
  *Verified: 45 tests green, ruff clean, `ironcore doctor` runs on Windows.*

## Phase 1 — Foundation

- [x] **IC-101 · Config hardening + doctor depth** *(done: fable-session, 2026-07-15 — ConfigError w/ path+line, mode validation, IRONCORE_ROLE_* env, doctor roles + non-localhost warning; 18 targeted tests)*
  - **Depends:** —  · **Spec:** SPEC §12, CONTRACTS §7
  - **Files:** `ironcore/config/settings.py`, `ironcore/cli.py`, `tests/test_config.py`, `tests/test_smoke.py`
  - **Build:** friendly TOML-error reporting (file+line, no traceback); `IRONCORE_ROLE_*` env
    overrides; mode-value validation at load (bad mode → clear error listing valid modes);
    doctor: report per-role models, warn on hosted endpoint + network_tools (SAFETY §6).
  - **Accept:** malformed TOML → exit 1 with path+line; invalid `safety.mode` rejected;
    doctor output covers all checks. **Verify:** `uv run --extra dev pytest tests/test_config.py tests/test_smoke.py -q`

- [x] **IC-102 · Session state store** *(done: fable-session, 2026-07-15 — SessionState dataclass, atomic os.replace save, corrupt→fresh+warning; 13 tests)*
  - **Depends:** — · **Spec:** ARCHITECTURE §5
  - **Files:** `ironcore/core/state.py` (new), `tests/test_state.py` (new)
  - **Build:** `SessionState` dataclass (mode, goal, working-set paths, plan steps + cursor,
    turn counter, budgets spent) with atomic JSON save/load to `.ironcore/state.json`
    (write-temp-rename); corrupt file → fresh state + warning, never a crash.
  - **Accept:** roundtrip preserves all fields; interrupted-write simulation recovers.
    **Verify:** `uv run --extra dev pytest tests/test_state.py -q`

- [x] **IC-103 · Audit trail writer** *(done: fable-session, 2026-07-15 — stdlib-only AuditWriter, day-file JSONL, sha256+120-char previews, no-rewrite-API pinned by introspection; 15 tests)*
  - **Depends:** — · **Spec:** SAFETY §5
  - **Files:** `ironcore/safety/audit.py` (new), `tests/test_audit.py` (new)
  - **Build:** append-only JSONL under `.ironcore/audit/YYYY-MM-DD.jsonl`; event types
    tool_call/gate/approval/mode_change/turn_end; args stored as sha256 hash + short preview;
    stdlib only (safety package rule).
  - **Accept:** lines parse as JSON; no rewrite API exists; preview never exceeds 120 chars.
    **Verify:** `uv run --extra dev pytest tests/test_audit.py -q`

- [x] **IC-104 · MockProvider failure injection + transcript fixtures** *(done: fable-session, 2026-07-15 — MalformedToolJSON/Truncate/TimeoutFailure/RaiseError markers, from_fixture JSONL loader, stream always terminates done|error; 16 new tests, happy path byte-compatible)*
  - **Depends:** — · **Spec:** SPEC §14
  - **Files:** `ironcore/providers/mock.py`, `tests/test_providers.py`, `tests/fixtures/` (new)
  - **Build:** scriptable failures: malformed-tool-JSON event, mid-stream truncation, timeout,
    `ProviderError`; JSONL transcript fixture loader (`MockProvider.from_fixture(path)`).
  - **Accept:** each failure mode reproducible in a test; fixture roundtrip works.
    **Verify:** `uv run --extra dev pytest tests/test_providers.py -q`

## Phase 2 — Providers

- [x] **IC-201 · OpenAI-compatible client (streaming, retries)** *(done: fable-session, 2026-07-15 — httpx client, backoff+jitter+Retry-After, ProviderError w/ key redaction, transport/sleep seams; 17 tests)*
  - **Depends:** IC-104 · **Spec:** SPEC §8.1, CONTRACTS §2, module docstring in `openai_compat.py`
  - **Files:** `ironcore/providers/openai_compat.py`, `tests/providers/test_openai_compat.py` (new)
  - **Build:** httpx.AsyncClient; SSE parse; retry/backoff+jitter on 429/5xx honoring
    Retry-After; `ProviderError` with redacted messages; `list_models` via `/models`.
  - **Accept:** httpx.MockTransport tests: happy stream, 429-then-success, timeout, key never
    in any error string. **Verify:** `uv run --extra dev pytest tests/providers -q`

- [x] **IC-202 · Native tool-call parsing across chunk fragments** *(done: fable-session, 2026-07-15 — SSE parse, per-index fragment accumulation, flush-at-end, malformed→repairable error event; 11 tests)*
  - **Depends:** IC-201 · **Spec:** SPEC §8.1
  - **Files:** `ironcore/providers/openai_compat.py`, `tests/providers/test_toolcalls.py` (new)
  - **Build:** accumulate `tool_calls` deltas by index; emit `StreamEvent(kind="tool_call")`
    only when arguments JSON parses complete; malformed-at-finish → repairable error event.
  - **Accept:** fragmented-across-chunks call reassembles; two parallel calls both emit;
    garbage args → `repairable: true` event, no exception. **Verify:** same as IC-201

- [x] **IC-203 · Ollama extras** *(done: fable-session, 2026-07-15 — /api/tags discovery, /api/show ModelDetails for the envelope, keep_alive injection, num_ctx warning, graceful non-Ollama fallback; 21 tests)*
  - **Depends:** IC-201 · **Spec:** SPEC §8.2, MODELS §7
  - **Files:** `ironcore/providers/ollama.py` (new), `tests/providers/test_ollama.py` (new)
  - **Build:** subclass adding `/api/tags` discovery, `/api/show` context-length+quant
    introspection (feeds envelope), keep_alive management, `num_ctx` mismatch warning.
  - **Accept:** MockTransport tests for all three; degrades gracefully on non-Ollama endpoints.
    **Verify:** `uv run --extra dev pytest tests/providers -q`

- [x] **IC-204 · Provider registry + role routing** *(done: fable-session, 2026-07-15 — model-keyed instance cache, for_role fallback-to-default, idempotent async close_all, factory/transport seams; 11 tests)*
  - **Depends:** IC-201 · **Spec:** SPEC §4.4, §12
  - **Files:** `ironcore/providers/registry.py` (new), `tests/providers/test_registry.py` (new)
  - **Build:** build provider(s) from Settings; `for_role("planner"|...)` returns the routed
    or default provider; single shared transport; clean `close_all()`.
  - **Accept:** role fallback to default; same base_url reuses one client.
    **Verify:** `uv run --extra dev pytest tests/providers -q`

- [x] **IC-205 · Endpoint capability detection** *(done: fable-session, 2026-07-15 — EndpointFeatures via one-knob probes, server_hint heuristics, dead-endpoint→all-False never-raise, as_priors below ladder thresholds; 20 tests)*
  - **Depends:** IC-201 · **Spec:** SPEC §8.3
  - **Files:** `ironcore/providers/detect.py` (new), `tests/providers/test_detect.py` (new)
  - **Build:** feature-probe an endpoint: native `tools` accepted? `format=json`/grammar/
    guided-decoding? logprobs? → `EndpointFeatures` dataclass consumed as envelope priors.
  - **Accept:** detection is one short request per feature, all mockable; unknown endpoints
    → all-False, never an exception. **Verify:** `uv run --extra dev pytest tests/providers -q`

## Phase 3 — Tools

- [x] **IC-301 · Read-side tools: read_file / list_dir / glob / grep** *(done: fable-session, 2026-07-15 — 4 READ tools, line-numbered reads w/ exact truncation counts, binary-safe grep, honest caps; 25 tests)*
  - **Depends:** — · **Spec:** SPEC §6.1–6.2
  - **Files:** `ironcore/tools/fs_read.py` (new), `tests/tools/test_fs_read.py` (new)
  - **Build:** READ-risk tools; line-numbered reads with offset/limit; honest `[truncated:
    N more lines]` markers; grep via `re` over workspace files (binary-safe skip); model-facing
    descriptions with one example each.
  - **Accept:** truncation marks exact counts; binary files skipped not crashed; all outputs
    deterministic. **Verify:** `uv run --extra dev pytest tests/tools -q`

- [x] **IC-302 · Deterministic patcher + write-side tools** *(done: fable-session, 2026-07-15 — 3 pure appliers (unified-diff ±3 fuzz, unique-match search/replace, whole-file guard+no-op), jail-routed atomic WriteFileTool/EditFileTool, CRLF/LF preserved, byte-unchanged on failure; 54 tests)*
  - **Depends:** IC-301 · **Spec:** SPEC §4.3, §6.1; MODELS §3
  - **Files:** `ironcore/tools/patch.py` (new), `ironcore/tools/fs_write.py` (new), `tests/tools/test_patch.py` (new)
  - **Build:** three appliers — unified diff (fuzzy line-anchor match ±3), search/replace
    blocks (unique-match required; on ambiguity report candidate contexts), whole-file (size
    guard, no-op detect); WRITE tools route through the applier; a failed apply returns the
    mechanical reason formatted for the repair loop (SPEC §5.4).
  - **Accept:** table-driven fixtures per format incl. offset-drift, ambiguous-match,
    CRLF/LF preservation; nothing writes on failed apply. **Verify:** `uv run --extra dev pytest tests/tools -q`

- [x] **IC-303 · Shell tool (Windows + POSIX)** *(done: fable-session, 2026-07-15 — EXEC tool, process-tree kill via killpg/taskkill, merged capped output, exact command in data for previews; 10 cross-OS tests)*
  - **Depends:** — · **Spec:** SPEC §6.1
  - **Files:** `ironcore/tools/shell.py` (new), `tests/tools/test_shell.py` (new)
  - **Build:** EXEC-risk; asyncio subprocess; cwd=workspace; timeout kills the process tree
    (psutil-free: platform APIs); merged output with cap + `[truncated]`; exit code in
    `ToolResult.data`; resolved command echoed for approval previews.
  - **Accept:** tests pass on Windows AND ubuntu CI (echo, exit-code, timeout-kill, big-output
    cap). **Verify:** `uv run --extra dev pytest tests/tools -q`

- [x] **IC-304 · Default toolset assembly** *(done: fable-session, 2026-07-15 — build_default_registry: 7 local tools always + fetch_url when network_tools; every spec JSON-schema-valid + example-bearing; added the minimal NET fetch tool the matrix needs; 14 tests)*
  - **Depends:** IC-301, IC-302, IC-303 · **Spec:** SPEC §6.1
  - **Files:** `ironcore/tools/default.py` (new), `ironcore/tools/fetch.py` (new — minimal NET fetch_url, scoped addition), `ironcore/tools/__init__.py` (additive exports), `tests/tools/test_default.py` (new)
  - **Build:** `build_default_registry(settings, workspace)` — fs + shell always; fetch_url
    only when `safety.network_tools`; every schema has model-facing description + example.
  - **Accept:** registry contents match settings matrix; all specs JSON-schema-valid.
    **Verify:** `uv run --extra dev pytest tests/tools -q`

## Phase 4 — Safety kernel completion

- [x] **IC-401 · Path jail** *(done: fable-session, 2026-07-15 — resolve_jailed/is_inside, blocks ../absolute/UNC/drive/symlink-escape via resolved-realpath containment; 16 tests, 25 escape attempts)*
  - **Depends:** — · **Spec:** SAFETY §2 T2, SPEC §7.3
  - **Files:** `ironcore/safety/jail.py` (new), `tests/test_jail.py` (new)
  - **Build:** `resolve_jailed(workspace, candidate) -> Path | JailViolation`; blocks `..`,
    absolute escapes, symlink escape (resolve-then-check), Windows drive/UNC/8.3 tricks;
    stdlib only.
  - **Accept:** adversarial table of ≥15 escape attempts all blocked on both OSes; legit
    nested paths pass. **Verify:** `uv run --extra dev pytest tests/test_jail.py -q`

- [x] **IC-402 · Command policy engine** *(done: fable-session, 2026-07-15 — normalize+unwrap shell wrappers, deny-list in ALL modes, risky-pattern ALLOW→ASK-in-AUTO, additive-only ceiling merge; policy.py DENYLIST/RISKY seeds extended; tests)*
  - **Depends:** — · **Spec:** SAFETY §2 T1/T8, SPEC §7.4, CONTRACTS §7
  - **Files:** `ironcore/safety/commands.py` (new), `ironcore/safety/policy.py`, `tests/test_command_policy.py` (new)
  - **Build:** normalize command line (quotes, `cmd /c`, `sh -c` unwrap); deny-list match in
    ALL modes; risky-pattern classifier (pushes, publishes, recursive deletes, privilege
    escalation, pipe-to-shell) escalating ALLOW→ASK in AUTO; config may add rules but only
    tighten; project config cannot loosen user ceiling.
  - **Accept:** deny-list bypass attempts (quoting, prefixing) still caught; tighten-only
    property tested. **Verify:** `uv run --extra dev pytest tests/test_command_policy.py -q`

- [x] **IC-403 · Approval flow plumbing** *(done: fable-session, 2026-07-15 — ApprovalBroker async request/answer, turn-scoped grants auto-expiring at turn end, timeout→deny fail-closed, audited; 11 tests)*
  - **Depends:** IC-102 · **Spec:** SPEC §3.1, SAFETY §4, CONTRACTS §4
  - **Files:** `ironcore/core/approvals.py` (new), `tests/test_approvals.py` (new)
  - **Build:** `ApprovalBroker`: engine awaits `request(preview) -> ApprovalAnswer`; front end
    answers via future; timeout → deny; "approve all writes this turn" scoped grant that
    auto-expires at turn end; every answer audited (IC-103).
  - **Accept:** async tests: grant, deny, timeout-deny, turn-scoped-grant expiry.
    **Verify:** `uv run --extra dev pytest tests/test_approvals.py -q`

- [x] **IC-404 · Secret redaction** *(done: fable-session, 2026-07-15 — Redactor.from_env + key-shaped patterns (sk-/ghp_/AKIA/Bearer/PEM), 3 choke-point fns, 1MB in ~5ms; 18 tests, 10 planted secrets all caught)*
  - **Depends:** — · **Spec:** SAFETY §6
  - **Files:** `ironcore/safety/redact.py` (new), `tests/test_redact.py` (new)
  - **Build:** `Redactor` built from env values (len≥8), `.env` values, key-shaped patterns
    (sk-/ghp_/AKIA/PEM); single-pass replace with `[redacted:<label>]`; applied at the three
    choke points (outbound context, transcript, audit) — expose one function each.
  - **Accept:** fixture text with 10 planted secrets → zero survive; no catastrophic-regex
    blowup on 1MB input (<100ms). **Verify:** `uv run --extra dev pytest tests/test_redact.py -q`

- [x] **IC-405 · Git snapshot undo engine** *(done: fable-session, 2026-07-15 — shadow ref refs/ironcore/undo via private GIT_INDEX_FILE, non-git private-repo fallback, byte-exact undo/redo incl adds/deletes, transparent to user index/HEAD/branch; 9 tests)*
  - **Depends:** — · **Spec:** SAFETY §5, SPEC §7.6
  - **Files:** `ironcore/safety/snapshots.py` (new), `tests/test_snapshots.py` (new)
  - **Build:** shadow snapshots on `refs/ironcore/undo` (user repo) or a private repo under
    `.ironcore/snapshots/` (non-git workspaces); `snapshot(label)`, `undo()`, `redo()`,
    byte-exact restore incl. deletions; never touches user index/branches/worktree config.
  - **Accept:** snapshot→mutate→undo→redo roundtrip byte-identical in git and non-git tmp
    workspaces; user's `git status` unchanged by snapshotting. **Verify:** `uv run --extra dev pytest tests/test_snapshots.py -q`

- [x] **IC-406 · Injection guard** *(done: fable-session, 2026-07-15 — nonce-delimited wrap_untrusted, two-tier detect_injection (12/12 flagged, 0 benign FP), downgrade_for_flag tighten-only HOT/SUSPICIOUS→ASK in AUTO; 29 tests)*
  - **Depends:** — · **Spec:** SAFETY §2 T3, SPEC §7.5
  - **Files:** `ironcore/safety/injection.py` (new), `tests/test_injection.py` (new)
  - **Build:** `wrap_untrusted(text, source)` with delimiters + standing DATA-not-instructions
    rule text; heuristic detector (imperatives addressed to the agent, tool-syntax lookalikes,
    "ignore previous" family) → flag levels none/suspicious/hot; hot in AUTO downgrades next
    gate to ASK (hook consumed by IC-502).
  - **Accept:** corpus of 12 injection samples flags ≥10, benign corpus flags ≤1; wrapper
    delimiters collision-safe (nonce). **Verify:** `uv run --extra dev pytest tests/test_injection.py -q`

## Phase 5 — Turn engine

- [x] **IC-501 · Context composer** *(done: fable-session, 2026-07-16 — pure compose(): system+anchor(system msg)+MRU working-set+history+input, SPEC §4.3 budget shares vs honest_context, anchor per cadence/plan-active, redact_context on untrusted content; 20 tests)*
  - **Depends:** IC-102 · **Spec:** SPEC §5.2, §4.3; CONTRACTS §4
  - **Files:** `ironcore/core/composer.py` (new), `tests/test_composer.py` (new)
  - **Build:** pure function state→messages: per-envelope system template (native vs ironcall
    variants), anchor block (goal/constraints/mode/current step), working-set excerpts
    (MRU, budgeted), compacted history, input; budget shares from SPEC §4.3 against
    `honest_context`; approximate tokenizer (chars/4) isolated behind one function.
  - **Accept:** deterministic given state; never exceeds budget (property test over random
    states); anchor present exactly per cadence. **Verify:** `uv run --extra dev pytest tests/test_composer.py -q`

- [x] **IC-502 · Turn state machine** *(done: fable-session, 2026-07-16 — full COMPOSE→CALL→PARSE→GATE→EXECUTE→OBSERVE→VERIFY→DONE loop, native+ironcall protocols, gate=decide→command-policy→jail-read(SAFETY-T4)→injection-downgrade, snapshot-per-mutating-turn, evidence-based stop_reason, 4 collaborator seams in protocols.py; 14 event-sequence tests)*
  - **Depends:** IC-501, IC-403, IC-304, IC-104 · **Spec:** SPEC §5, CONTRACTS §4
  - **Files:** `ironcore/core/engine.py`, `tests/test_engine.py` (new)
  - **Build:** implement `run_turn`: COMPOSE→CALL→PARSE→GATE→EXECUTE→OBSERVE loop→DONE
    emitting events; gate via `decide()` + command policy + injection downgrade hook;
    tool output truncate/redact/wrap before OBSERVE; deny feeds framed refusal to model.
  - **Accept:** MockProvider-scripted sessions assert full event sequences: text-only turn,
    tool turn, ask→deny turn, deny-in-plan turn. **Verify:** `uv run --extra dev pytest tests/test_engine.py -q`

- [x] **IC-503 · Repair loops** *(done: fable-session, 2026-07-16 — LadderRepairPolicy stateless decision table (RETRY→LADDER_DOWN→GIVE_UP, floor gives up, max_attempts backstop) + frame_error, wired as engine default incl. framed re-ask; 14 tests)*
  - **Depends:** IC-502 · **Spec:** SPEC §5.4
  - **Files:** `ironcore/core/repair.py` (new), `ironcore/core/engine.py`, `tests/test_repair.py` (new)
  - **Build:** on repairable parse/apply failure: re-ask once with mechanical error framed;
    second failure → ladder-down for the rest of the turn (session-sticky option in state);
    all repairs budgeted + emitted as events.
  - **Accept:** scripted malformed-then-fixed passes; malformed-twice ladders down; repair
    budget trips cleanly. **Verify:** `uv run --extra dev pytest tests/test_repair.py -q`

- [x] **IC-504 · Verification loop** *(done: fable-session, 2026-07-16 — CommandVerifier: discovery (configured>IRONCORE.md>auto-detect pytest/npm/cargo) + subprocess run w/ tail-on-fail, engine default; orchestrator wired the feed-failures-back-once re-loop; 15 tests)*
  - **Depends:** IC-502 · **Spec:** SPEC §5.5, SAFETY §2 T7
  - **Files:** `ironcore/core/verify.py` (new), `ironcore/core/engine.py`, `tests/test_verify.py` (new)
  - **Build:** verify-command discovery (IRONCORE.md → /goal verify: → auto-detect pytest/
    npm-test/cargo-test); run after WRITE/EXEC turns; one feedback round; `stop_reason`
    strictly evidence-based.
  - **Accept:** failing-then-passing fixture converges; still-failing surfaces honestly with
    output; no-verify-command turns skip cleanly. **Verify:** `uv run --extra dev pytest tests/test_verify.py -q`

- [x] **IC-505 · Micro-stepping + compaction** *(done: fable-session, 2026-07-16 — PlanStepPlanner evidence-gated advance + set_plan (engine default), compact()→handoff-grade summary w/ mechanical fallback + should_compact; orchestrator wired the compaction trigger; 24 tests)*
  - **Depends:** IC-502 · **Spec:** SPEC §5.3, §11.2
  - **Files:** `ironcore/core/steps.py` (new), `ironcore/core/compact.py` (new), tests (new)
  - **Build:** plan holder (steps + cursor + evidence per done step) surfaced through the
    anchor block; compaction at context pressure → handoff-grade summary (memory.Handoff
    fields) via summarizer-role model, with mechanical fallback (truncate + working-set list)
    if the model call fails.
  - **Accept:** step cursor advances only on evidence; compaction output parses as Handoff;
    fallback path tested. **Verify:** `uv run --extra dev pytest tests/test_steps.py tests/test_compact.py -q`

- [x] **IC-506 · Budgets + runaway protection** *(done: fable-session, 2026-07-16 — Budget: per-turn+session caps (calls/tokens/wallclock via injected clock/repairs) + 2x-warn/3x-stop loop detector + summary(), engine default; 15 tests)*
  - **Depends:** IC-502 · **Spec:** SPEC §5.6, SAFETY §2 T5
  - **Files:** `ironcore/core/budgets.py` (new), `ironcore/core/engine.py`, `tests/test_budgets.py` (new)
  - **Build:** per-turn/session caps (calls, tokens, wall-clock, repairs) from settings;
    loop detector (identical tool+args 2x → intervention frame, 3x → stop); clean
    `stop_reason="budget"` with state summary.
  - **Accept:** each cap trips in scripted tests; loop detector catches and stops.
    **Verify:** `uv run --extra dev pytest tests/test_budgets.py -q`

## Phase 6 — Envelope

- [x] **IC-601 · Probe runner + report card** *(done: fable-session, 2026-07-16 — Probe protocol (id/title/targets/async run→ProbeResult), run_probes dotted-path merge + degrade-on-failure-to-floor, probe_and_save, render_report_card; 12 tests)*
  - **Depends:** IC-201 · **Spec:** SPEC §4.1, MODELS §2
  - **Files:** `ironcore/envelope/runner.py` (new), `tests/test_probe_runner.py` (new)
  - **Build:** orchestrate PROBES against a Provider; partial failure → score 0 + note, never
    abort; write profile via `CapabilityProfile.save`; plain-text report card renderer
    (consumed later by `/envelope`).
  - **Accept:** full run against MockProvider produces a saved profile + card; one probe
    crashing doesn't sink the run. **Verify:** `uv run --extra dev pytest tests/test_probe_runner.py -q`

- [x] **IC-602 · CTX-HONESTY + RETENTION probes** *(done: fable-session, 2026-07-16 — needle-at-depth honest_context (contiguous-passing-prefix), constraint-retention→instruction_retention+coherence_horizon, mechanical scoring, graceful degrade; 17 tests)*
  - **Depends:** IC-601 · **Spec:** MODELS §2
  - **Files:** `ironcore/envelope/probe_ctx.py` (new), `tests/test_probe_ctx.py` (new)
  - **Build:** needle-at-depth ladder (4k→advertised, depths 25/50/75/90%); constraint-
    retention over filler turns scoring turns 3/6/9/12 → `honest_context`,
    `instruction_retention`, `coherence_horizon`.
  - **Accept:** MockProvider scripted "forgets past 8k" yields honest_context 8k; retention
    math verified against hand-computed fixtures. **Verify:** `uv run --extra dev pytest tests/test_probe_ctx.py -q`

- [x] **IC-603 · TOOL-FORM + JSON-STRICT probes** *(done: fable-session, 2026-07-16 — 10-trial per-protocol scoring (native/strict_json/ironcall via parse) + schema-conforming JSON under distractors→json_adherence; 18 tests)*
  - **Depends:** IC-601, IC-606 · **Spec:** MODELS §2
  - **Files:** `ironcore/envelope/probe_tools.py` (new), `tests/test_probe_tools.py` (new)
  - **Build:** 10 trials per protocol (native/strict_json/ironcall) with exact-match scoring
    (parseable + right name + right args); JSON-schema conformance trials with distractor
    instructions embedded in payload text.
  - **Accept:** scoring is mechanical + deterministic on scripted outputs; per-protocol scores
    land in profile. **Verify:** `uv run --extra dev pytest tests/test_probe_tools.py -q`

- [x] **IC-604 · EDIT-FORMAT + CODE-SMOKE probes** *(done: fable-session, 2026-07-16 — per-format apply-and-ast-parse scoring (no-op=fail) via IC-302 appliers + in-process code-smoke floor gate w/ isolated-globals exec; 22 tests)*
  - **Depends:** IC-601, IC-302 · **Spec:** MODELS §2
  - **Files:** `ironcore/envelope/probe_edits.py` (new), `tests/test_probe_edits.py` (new)
  - **Build:** per-format fixture edits scored by "IC-302 patcher applies AND result parses"
    (`ast.parse` for py fixtures); CODE-SMOKE floor gate (function-from-docstring passes its
    test in a tmp venv-free harness — pure-python fixture, exec in namespace).
  - **Accept:** deterministic on scripted outputs; whole-file no-op detected as failure.
    **Verify:** `uv run --extra dev pytest tests/test_probe_edits.py -q`

- [x] **IC-605 · Adapter wiring into the engine** *(done: fable-session, 2026-07-16 — merged into IC-502: engine consumes profile.recommended_tool_protocol (native vs ironcall floor) + recommended_edit_format + anchor_cadence + resolve_sampling(kind,attempt))*
  - **Depends:** IC-502, IC-601 · **Spec:** SPEC §4.3, CONTRACTS §5
  - **Files:** `ironcore/core/engine.py`, `ironcore/core/composer.py`, `tests/test_adapter_wiring.py` (new)
  - **Build:** engine consumes `recommended_tool_protocol()` / `recommended_edit_format()` /
    `anchor_cadence()` end-to-end; unprobed model → floor behavior; profile hot-swap on
    `/model` switch.
  - **Accept:** same scripted task runs in native mode and ironcall mode by swapping profile
    only. **Verify:** `uv run --extra dev pytest tests/test_adapter_wiring.py -q`

- [x] **IC-606 · IRONCALL text protocol** *(done: fable-session, 2026-07-16 — render_system_fragment (catalog+2 examples), parse()→IroncallParse (never raises, precise repair errors, first-of-multiple+warning), render_result; 31 tests)*
  - **Depends:** — · **Spec:** SPEC §6.3, CONTRACTS §10
  - **Files:** `ironcore/core/ironcall.py` (new), `tests/test_ironcall.py` (new)
  - **Build:** encoder (system-prompt fragment with 2 worked examples, tool docs renderer)
    + parser (fence regex, json.loads, precise error strings for the repair loop) +
    ironresult renderer; multiple blocks in one reply → first only + warning.
  - **Accept:** fuzz-ish table: nested fences, prose around blocks, invalid JSON, wrong tool
    name — all produce correct parse/err; roundtrip with ToolRegistry schemas.
    **Verify:** `uv run --extra dev pytest tests/test_ironcall.py -q`

- [x] **IC-607 · Sampling policies + best-of-n verifier harness** *(done: fable-session, 2026-07-16 — resolve_sampling per-kind bands (tool/edit cold, brainstorm warm) + retry temp bump, best_of short-circuit + budget duck-type; 20 tests)*
  - **Depends:** IC-502 · **Spec:** MODELS §6
  - **Files:** `ironcore/core/sampling.py` (new), `tests/test_sampling.py` (new)
  - **Build:** per-envelope SamplingPolicy resolution (tool turns cold, retries +0.2);
    `best_of(n, generate, verify)` used only where a mechanical verifier exists (patch
    applies / tests pass); budget-aware.
  - **Accept:** retry temperature bump verified; best_of returns first verified winner and
    respects budget. **Verify:** `uv run --extra dev pytest tests/test_sampling.py -q`

## Phase 7 — TUI

- [x] **IC-701 · Textual shell** *(done: fable-session, 2026-07-16 — IronCoreApp: transcript/input/status regions, engine event-stream consumer, Esc interrupt via worker cancel, TTY-gated launch; 11 Pilot tests)*
  - **Depends:** IC-502 · **Spec:** SPEC §3.1
  - **Files:** `ironcore/tui/app.py` (new), `ironcore/tui/widgets/` (new), `ironcore/cli.py`, `tests/tui/test_app.py` (new)
  - **Build:** app layout (transcript / input / status bar); engine wired via event consumer
    task; Esc interrupts; `ironcore` launches the app (doctor/`--version` unchanged).
  - **Accept:** Textual Pilot: boots, renders a MockProvider text turn, Esc interrupts.
    **Verify:** `uv run --extra dev pytest tests/tui -q`

- [x] **IC-702 · Streaming markdown + tool cards** *(done: fable-session, 2026-07-16 — incremental TextDelta rendering + ToolCard w/ risk chip + gate state + collapsed result; part of IC-701)*
  - **Depends:** IC-701 · **Spec:** SPEC §3.1
  - **Files:** `ironcore/tui/widgets/transcript.py`, `tests/tui/test_transcript.py` (new)
  - **Build:** incremental markdown rendering (no full-reparse-per-token jank); tool cards
    with risk chip + gate state + elapsed + collapsed output (enter expands); dimmed repair
    entries.
  - **Accept:** Pilot: streamed text appears incrementally; card lifecycle renders all states.
    **Verify:** `uv run --extra dev pytest tests/tui -q`

- [x] **IC-703 · Shift+Tab modes + approval modal** *(done: fable-session, 2026-07-16 — mode chip + Shift+Tab CYCLE, ApprovalScreen y/n/a→ApprovalAnswer w/ Deny-default-focus for EXEC/NET; part of IC-701)*
  - **Depends:** IC-701, IC-403 · **Spec:** SPEC §3.1–3.2, SAFETY §4
  - **Files:** `ironcore/tui/widgets/modebar.py`, `ironcore/tui/screens/approval.py`, `tests/tui/test_modes_approvals.py` (new)
  - **Build:** Shift+Tab cycles `safety.modes.CYCLE` with chip + transcript announcement;
    approval modal (exact diff/command preview, y/n/a keys, EXEC/NET never default-focused
    on approve) answering the ApprovalBroker.
  - **Accept:** Pilot: shift+tab cycles all four; scripted ask-gate shows modal; `n` denies
    and the turn continues with the refusal. **Verify:** `uv run --extra dev pytest tests/tui -q`

- [x] **IC-704 · Slash palette + completion** *(done: fable-session, 2026-07-16 — registry-driven dispatch w/ ctx.extra {app,engine,registry,workspace,provider_registry,settings,schedule}; part of IC-701)*
  - **Depends:** IC-701 · **Spec:** SPEC §3.3
  - **Files:** `ironcore/tui/widgets/inputbar.py`, `tests/tui/test_palette.py` (new)
  - **Build:** `/` opens command palette from the registry (live + `[planned]` labels);
    tab-completion; unknown command → helpful error with nearest match; input history.
  - **Accept:** Pilot: `/he<tab>` completes `/help`; dispatch output lands in transcript.
    **Verify:** `uv run --extra dev pytest tests/tui -q`

- [x] **IC-705 · Diff viewer** *(done: fable-session, 2026-07-16 — DiffView scrollable +/- colored renderer, plugged into ApprovalScreen (write/diff-shaped) w/ plain fallback + inline ToolCard diffs; 8 tests)*
  - **Depends:** IC-702 · **Spec:** SPEC §3.1
  - **Files:** `ironcore/tui/widgets/diffview.py`, `tests/tui/test_diffview.py` (new)
  - **Build:** side-scrollable unified-diff rendering with syntax-aware coloring; used by
    approval modal and `/review`; per-file approve/reject in multi-file change sets.
  - **Accept:** Pilot renders a 3-file fixture diff; per-file reject excludes that file from
    apply. **Verify:** `uv run --extra dev pytest tests/tui -q`

- [x] **IC-706 · Session picker + resume** *(done: fable-session, 2026-07-16 — SessionPicker modal (newest-first, age/label/turns), app records user+assistant lines to SessionStore, `ironcore --resume [id]` rehydrates transcript+engine._conversation; 10 tests)*
  - **Depends:** IC-701, IC-1001 · **Spec:** SPEC §11.2
  - **Files:** `ironcore/tui/screens/sessions.py`, `tests/tui/test_sessions.py` (new)
  - **Build:** `ironcore --resume` / picker screen listing `.ironcore/sessions/` (age, first
    prompt, turn count); resume rehydrates state + transcript tail.
  - **Accept:** Pilot: create session, relaunch, resume shows prior transcript tail and
    continues with state intact. **Verify:** `uv run --extra dev pytest tests/tui -q`

## Phase 8 — Slash commands

- [x] **IC-801 · /model** *(done: fable-session, 2026-07-16 — list endpoint models (async) / switch settings.provider.model, graceful without a live endpoint)*
  - **Depends:** IC-204 · **Spec:** SPEC §3.3
  - **Files:** `ironcore/commands/builtins.py`, `tests/test_commands.py`
  - **Build:** list endpoint models (marking probed ones), switch model (loads/queues probe if
    unprofiled), update session state; keep handler non-blocking (schedule the probe).
  - **Accept:** switch reflected in state + status bar data; unprofiled switch queues probe.
    **Verify:** `uv run --extra dev pytest tests/test_commands.py -q`

- [x] **IC-802 · /init → IRONCORE.md** *(done: fable-session, 2026-07-16 — marker scan (py/node/rust/go/make) → build/verify cmds + structure map + sentinel-preserved user section; Verify section is CommandVerifier-parseable)*
  - **Depends:** IC-301 · **Spec:** SPEC §11.1
  - **Files:** `ironcore/commands/initcmd.py` (new), `tests/test_initcmd.py` (new)
  - **Build:** repo scan (manifests, test configs, layout) → IRONCORE.md skeleton (build/test
    commands, structure map, conventions); merge-safe re-runs (user sections preserved via
    sentinel comments).
  - **Accept:** fixture repos (py/node) produce correct commands; re-run preserves a
    hand-edited section. **Verify:** `uv run --extra dev pytest tests/test_initcmd.py -q`

- [x] **IC-803 · /goal engine** *(done: fable-session, 2026-07-16 — sets ctx.goal+engine.state.goal (composer anchors it), `verify:` attaches checks, `check` runs the stop-condition via CommandVerifier (async), show/clear)*
  - **Depends:** IC-502, IC-504 · **Spec:** SPEC §3.4
  - **Files:** `ironcore/commands/goalcmd.py` (new), `ironcore/core/engine.py`, `tests/test_goal.py` (new)
  - **Build:** goal in state + anchor injection; on model-claims-done: stop-condition check
    (fresh verifier-role call + optional `/goal verify:` commands); unmet → gaps framed as
    next input (budget-bounded); met → auto-clear + summary.
  - **Accept:** scripted premature-victory session gets continued; met goal auto-clears;
    budget trip reports unmet items honestly. **Verify:** `uv run --extra dev pytest tests/test_goal.py -q`

- [x] **IC-804 · /loop** *(done: fable-session, 2026-07-16 — parse_interval (30s/5m/1h) + LoopSpec, register/status/stop; app-driven recurrence via optional register_loop/stop_loop hooks)*
  - **Depends:** IC-502 · **Spec:** SPEC §3.3
  - **Files:** `ironcore/commands/loopcmd.py` (new), `tests/test_loop.py` (new)
  - **Build:** `/loop 5m <prompt>` fixed interval; `/loop <prompt>` self-paced (model proposes
    next delay, clamped); `/loop stop`; loop turns run in the session's CURRENT mode (no
    autonomy smuggling); status line shows next fire.
  - **Accept:** fake-clock tests: fires, reschedules, stops; mode changes mid-loop respected.
    **Verify:** `uv run --extra dev pytest tests/test_loop.py -q`

- [x] **IC-805 · /compact /undo /redo** *(done: fable-session, 2026-07-16 — /compact distills engine._conversation (async), /undo+/redo via SnapshotStore w/ restored-label report, graceful non-git)*
  - **Depends:** IC-505, IC-405 · **Spec:** SPEC §3.3
  - **Files:** `ironcore/commands/builtins.py`, `tests/test_commands.py`
  - **Build:** wire to compaction and snapshot engines; `/undo` previews what will be restored
    before doing it; both announce results in transcript.
  - **Accept:** end-to-end in tmp workspace: edit → /undo → bytes restored → /redo.
    **Verify:** `uv run --extra dev pytest tests/test_commands.py -q`

- [x] **IC-806 · /review** *(done: fable-session, 2026-07-16 — git diff HEAD → verifier-role model under bug rubric → parsed findings (async), degrades to raw text / non-git message)*
  - **Depends:** IC-502, IC-705 · **Spec:** SPEC §3.3
  - **Files:** `ironcore/commands/reviewcmd.py` (new), `tests/test_review.py` (new)
  - **Build:** working-diff review via verifier-role model with a bug-focused rubric prompt;
    findings as structured list (file:line, severity, claim) rendered onto the diff view;
    zero findings says so plainly.
  - **Accept:** MockProvider-scripted review produces parsed findings; malformed model output
    degrades to raw-text display, never a crash. **Verify:** `uv run --extra dev pytest tests/test_review.py -q`

- [x] **IC-807 · /memory** *(done: fable-session, 2026-07-16 — view IRONCORE.md / a ## section, `add <text>` appends to the sentinel-guarded user section)*
  - **Depends:** IC-802 · **Spec:** SPEC §11.1
  - **Files:** `ironcore/commands/memorycmd.py` (new), `tests/test_memorycmd.py` (new)
  - **Build:** view IRONCORE.md sections, append a note (`/memory add <text>`), open in
    $EDITOR; token-budget warning when memory grows past its context share.
  - **Accept:** add/view roundtrip; oversize warning fires. **Verify:** `uv run --extra dev pytest tests/test_memorycmd.py -q`

## Phase 9 — Workflows

- [x] **IC-901 · Subagent runner** *(done: fable-session, 2026-07-16 — SubagentTask/Result, run_subagent(engine_factory) fresh-context bounded loop, extract_json + subset-schema validate_against w/ one mechanical retry; 16 tests)*
  - **Depends:** IC-502 · **Spec:** SPEC §10
  - **Files:** `ironcore/workflows/subagent.py` (new), `tests/test_subagent.py` (new)
  - **Build:** run one agent task: fresh composed context (role prompt + task + envelope-
    sized), bounded turn loop, structured output validated against a declared schema with one
    mechanical retry; result + transcript ref returned.
  - **Accept:** scripted subagent returns validated output; schema-fail retries once then
    errors cleanly. **Verify:** `uv run --extra dev pytest tests/test_subagent.py -q`

- [x] **IC-902 · Workflow YAML schema + loader** *(done: fable-session, 2026-07-16 — pydantic Workflow/Phase/Fanout/AgentSpec (exactly-one-kind), load_workflow via yaml.safe_load→WorkflowError, discover_workflows, interpolate; CONTRACTS §9 frozen; 26 tests)*
  - **Depends:** — · **Spec:** SPEC §10, `workflows/engine.py` sketch
  - **Files:** `ironcore/workflows/schema.py` (new), `tests/test_workflow_schema.py` (new), `docs/CONTRACTS.md` (§9 finalize)
  - **Build:** finalize schema (phases: fanout/foreach/reduce; `{{var}}` interpolation;
    output_schema refs); pydantic validation with actionable errors; add `pyyaml` dep;
    freeze in CONTRACTS §9 (same commit).
  - **Accept:** valid fixtures load; 8 invalid fixtures produce pointed errors.
    **Verify:** `uv run --extra dev pytest tests/test_workflow_schema.py -q`

- [~] **IC-903 · Orchestrator** *(claimed: fable-session, 2026-07-16)*
  - **Depends:** IC-901, IC-902 · **Spec:** SPEC §10
  - **Files:** `ironcore/workflows/engine.py`, `tests/test_workflow_engine.py` (new)
  - **Build:** execute phases sequentially, items concurrently (cap from settings); harness-
    only control flow; per-agent failure isolation (null + note, workflow continues);
    progress events onto the core event stream.
  - **Accept:** scripted 3-phase workflow (fanout→foreach→reduce) with one injected agent
    failure completes with the failure noted. **Verify:** `uv run --extra dev pytest tests/test_workflow_engine.py -q`

- [~] **IC-904 · /workflow command + progress UI** *(claimed: fable-session, 2026-07-16)*
  - **Depends:** IC-903, IC-702 · **Spec:** SPEC §3.3
  - **Files:** `ironcore/commands/workflowcmd.py` (new), `ironcore/tui/widgets/workflowview.py` (new), tests (new)
  - **Build:** list/run workflows from `.ironcore/workflows/`; first-run-per-repo confirmation
    summary (SAFETY T8); grouped progress tree in transcript; cancel.
  - **Accept:** Pilot: run fixture workflow, watch phases, cancel mid-fanout cleans up.
    **Verify:** `uv run --extra dev pytest tests/tui -q tests/test_commands.py -q`

- [~] **IC-905 · Built-in workflows** *(claimed: fable-session, 2026-07-16)*
  - **Depends:** IC-903 · **Spec:** SPEC §10
  - **Files:** `ironcore/workflows/builtin/*.yaml` (new), `tests/test_builtin_workflows.py` (new)
  - **Build:** `review` (dimensions fanout → verify → report), `migrate` (discover → transform
    → verify), `explain-repo` (subsystem readers → synthesis); each ≤1 page of YAML, schema-
    valid, MockProvider-runnable.
  - **Accept:** all three validate and complete against scripted providers.
    **Verify:** `uv run --extra dev pytest tests/test_builtin_workflows.py -q`

## Phase 10 — Memory & sessions

- [x] **IC-1001 · Session store** *(done: fable-session, 2026-07-16 — SessionStore JSONL transcripts: create/append/list(newest-first)/load/rehydrate→Messages+tail, corrupt-tolerant, prune-never-active, caller-stamped time; 20 tests)*
  - **Depends:** IC-102 · **Spec:** SPEC §11.2
  - **Files:** `ironcore/memory/sessions.py` (new), `tests/test_sessions.py` (new)
  - **Build:** JSONL transcript persistence (events + messages) under `.ironcore/sessions/`;
    list/load/rehydrate; size-capped with oldest-session pruning warning.
  - **Accept:** write→load roundtrip rehydrates state + tail; corrupt line skipped with
    warning. **Verify:** `uv run --extra dev pytest tests/test_sessions.py -q`

- [x] **IC-1002 · Handoff lifecycle wiring** *(done: fable-session, 2026-07-16 — handoff_from_summary parser + engine handoff_path/author params, compaction auto-appends a handoff, end_session() writes a final block, best-effort/decoupled (None-control byte-identical); 13 tests)*
  - **Depends:** IC-505, IC-1001 · **Spec:** SPEC §11.3
  - **Files:** `ironcore/memory/handoff.py`, `ironcore/core/engine.py`, `tests/test_handoff.py`
  - **Build:** auto-append handoff blocks on session end, compaction, and workflow-agent
    completion (author = session/agent id); `latest_handoff` surfaced on resume.
  - **Accept:** end/compact/subagent each produce parseable blocks; resume shows the latest.
    **Verify:** `uv run --extra dev pytest tests/test_handoff.py -q`

- [x] **IC-1003 · IRONCORE.md injection** *(done: fable-session, 2026-07-16 — load_project_memory (budget-capped/summarize-once-cached), compose budgets memory into the SYSTEM share (invariant held), orchestrator wired the per-turn engine call; 12 tests)*
  - **Depends:** IC-501, IC-802 · **Spec:** SPEC §11.1
  - **Files:** `ironcore/core/composer.py`, `tests/test_composer.py`
  - **Build:** project memory into the system share of the budget; oversize → summarize-once
    + cache; missing file → silent skip.
  - **Accept:** memory text present in composed context; oversize path exercised.
    **Verify:** `uv run --extra dev pytest tests/test_composer.py -q`

## Phase 11 — Distribution & v0.1

- [x] **IC-1101 · CI hardening** *(done: fable-session, 2026-07-16 — coverage gate --cov-fail-under=85 on core/safety/envelope (measured 91-94%), uv cache + --frozen sync, matrix/concurrency/smoke intact; pyproject [tool.coverage] added)*
  - **Depends:** — (tighten as phases land) · **Spec:** SPEC §14
  - **Files:** `.github/workflows/ci.yml`, `pyproject.toml`
  - **Build:** coverage gate (85% on core/safety/envelope once IC-502 lands), `uv lock` +
    cached installs, concurrency-cancel, badge already in README.
  - **Accept:** CI green on matrix with gates active. **Verify:** GitHub Actions run

- [~] **IC-1102 · Packaging + release automation** *(claimed: fable-session, 2026-07-16)*
  - **Depends:** IC-701 · **Spec:** SPEC §13
  - **Files:** `.github/workflows/release.yml` (new), `pyproject.toml`, `README.md`
  - **Build:** tag-triggered build + PyPI publish (trusted publishing); `uv tool install`
    / `pipx` paths verified in CI on ubuntu+windows; version single-sourcing check.
  - **Accept:** dry-run release produces installable wheel; `ironcore --version` correct from
    wheel. **Verify:** release workflow dry-run

- [~] **IC-1103 · Offline e2e demo** *(claimed: fable-session, 2026-07-16)*
  - **Depends:** IC-701..704 · **Spec:** SPEC §14
  - **Files:** `demo/` (new), `tests/test_demo.py` (new)
  - **Build:** scripted MockProvider session (fixture transcript): user asks for a small
    feature → plan → edits → verify → done, runnable headless (`python -m demo`) for CI and
    recordable for the README gif.
  - **Accept:** demo runs green in CI on both OSes. **Verify:** `uv run --extra dev pytest tests/test_demo.py -q`

- [~] **IC-1104 · v0.1 release** *(claimed: fable-session, 2026-07-16)*
  - **Depends:** all phase 1–8 + IC-1101..1103 · **Spec:** SPEC §15
  - **Files:** `CHANGELOG.md` (new), `README.md`, version bumps
  - **Build:** changelog; README status flip (scaffold → v0.1) + demo gif; live smoke against
    a real local Ollama documented in the handoff; tag `v0.1.0`.
  - **Accept:** tagged, CI-green, installable, README truthful. **Verify:** fresh
    `pipx install ironcore` on a clean machine runs the TUI

---

### Dependency quick-map

```
IC-101..104 (foundation, parallel-safe)
IC-201 → IC-202/203/204/205        IC-301 → IC-302 → ┐
IC-401/402/404/405/406 (parallel)  IC-303 ───────────┼→ IC-304 ┐
IC-102 → IC-403 ───────────────────────────────────────────────┼→ IC-501 → IC-502
IC-606 (anytime) → IC-603                                       ┘     │
IC-502 → IC-503/504/505/506/607 → IC-601..605 → engine complete      │
IC-502 → IC-701 → IC-702..706 → IC-801..807 → IC-901..905 → IC-1001..1003 → IC-1104
```
Parallel-safety rule: two tasks may run concurrently only if they share no Files and no
CONTRACTS.md sections (PROTOCOLS.md §5).
