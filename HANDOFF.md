# HANDOFF.md — append-only session handoffs (format: docs/PROTOCOLS.md §2)

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-15T20:05:00+00:00 — agent-ic101
**Context:** IC-101 config hardening + doctor depth, from the phase-1 wave.
**Changed:** ironcore/config/settings.py (ConfigError, mode validation, IRONCORE_ROLE_* env), ironcore/cli.py (doctor: injectable params, role report, non-localhost+network_tools [!!] warning), tests/test_config.py (+6), tests/test_smoke.py (+5).
**Verified:** uv run --extra dev pytest tests/test_config.py tests/test_smoke.py -q -> 18 passed; ruff clean; live `ironcore doctor` exit 0.
**Next:** IC-402 command policy should catch ConfigError (not Exception); CONTRACTS §7 additive env names IRONCORE_ROLE_*.
**Gotchas:** Mode is a StrEnum but hashes as enum member — validate with Mode(value), never `x in set(Mode)`. Mode validation runs after env application.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-15T20:05:00+00:00 — agent-ic102
**Context:** IC-102 session state store, phase-1 wave.
**Changed:** ironcore/core/state.py (new: SessionState, state_path, STATE_VERSION), tests/test_state.py (new, 13 tests).
**Verified:** uv run --extra dev pytest tests/test_state.py -q -> 13 passed; full suite green; ruff clean.
**Next:** IC-501/502 consume SessionState — call state.save(state_path(ws)) at turn boundaries; surface load()'s warning string to the user, never swallow.
**Gotchas:** load never raises: corrupt file -> (fresh state, warning) and discards everything, so boot should re-anchor from HANDOFF/user when warning is not None. Fields: mode/goal/working_set/plan_steps/plan_cursor/plan_evidence/turn_count/budgets_spent. working_set dedup is exact-string — composer must normalize Windows paths before touch().
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-15T20:05:00+00:00 — agent-ic103
**Context:** IC-103 audit trail writer, phase-1 wave.
**Changed:** ironcore/safety/audit.py (new, stdlib-only), tests/test_audit.py (new, 15 tests).
**Verified:** uv run --extra dev pytest tests/test_audit.py -q -> 15 passed; full suite 100 passed; ruff clean.
**Next:** IC-403/IC-502 write via typed helpers tool_call/gate/approval/mode_change/turn_end; fields pinned in the module (ts/session/turn/event + args_sha256/args_preview/decision/...). fingerprint_args(args) gives (sha256, <=120-char preview).
**Gotchas:** files open append-mode per write with newline="\n" (no CRLF translation); date rollover automatic per-write; json.dumps default=str so exotic values coerce, never crash the audit path.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-15T20:05:00+00:00 — agent-ic104
**Context:** IC-104 MockProvider failure injection + fixtures, phase-1 wave.
**Changed:** ironcore/providers/mock.py (markers MalformedToolJSON/Truncate/TimeoutFailure/RaiseError, from_fixture), ironcore/providers/__init__.py (additive exports), tests/test_providers.py (+16 tests, pre-existing tests byte-untouched), tests/fixtures/{basic,failure}_session.jsonl (new).
**Verified:** uv run --extra dev pytest tests/test_providers.py -q -> 20 passed; full suite 100; ruff clean.
**Next:** IC-201 must mirror the split: stream-mode transport failures = terminal non-repairable error EVENT; complete() raises ProviderError. Repairable convention: data has repairable+reason (+raw for malformed, +message for non-repairable).
**Gotchas:** mock.py imports ProviderError from openai_compat — once IC-201 adds module-level httpx there, MockProvider consumers transitively import httpx (fine: httpx is a core dep). Fixture tool_call streams end with done data {"finish_reason": "tool_calls"} (test-pinned).
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-15T21:10:00+00:00 — agent-ic201-202
**Context:** IC-201+IC-202 OpenAI-compatible provider (single owner, same file), phase-2 wave.
**Changed:** ironcore/providers/openai_compat.py (full impl), tests/providers/test_openai_compat.py (17 tests), tests/providers/test_toolcalls.py (11 tests), tests/test_providers.py (honest-stub test replaced by base-url-normalization pin only).
**Verified:** uv run --extra dev pytest -q -> 128 passed; ruff clean.
**Next:** IC-203/204/205 — reuse self._send_with_retries(method, path, json_body=, retries=, stream=); Ollama /api/* lives at server ROOT, strip /v1 from base_url; registry can share one httpx transport via the transport= kwarg (aclose is idempotent).
**Gotchas:** tool_call events flush at stream END only (on [DONE] or EOF), sorted by index; malformed accumulated JSON -> terminal repairable error event (no done after it); mid-stream transport death does NOT flush drafts; stream-mode connect failures yield error events while complete() raises ProviderError. Redaction is instance-level (_redact/_describe) — new subclass error paths must route through them.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-15T21:55:00+00:00 — agent-ic203
**Context:** IC-203 Ollama extras, phase-2 wave.
**Changed:** ironcore/providers/ollama.py (new: OllamaProvider, ModelInfo, ModelDetails), tests/providers/test_ollama.py (21 tests). openai_compat.py NOT touched (used the existing _request_body seam for keep_alive).
**Verified:** uv run --extra dev pytest -q -> 180 passed; ruff clean.
**Next:** IC-601 consumes ModelDetails verbatim (context_length/quantization/family/num_ctx_configured; None = unknown, floor-conservative). Registry should only construct OllamaProvider for Ollama-detected endpoints or pass keep_alive=None.
**Gotchas:** /api/* anchored at api_root (trailing /v1 stripped) via private _send_api — parent _send_with_retries would nest /api under /v1. ollama.py imports module-private _RETRY_STATUSES/_TRANSPORT_ERRORS/_backoff_delay from openai_compat — renames there must update ollama.py same-commit. num_ctx_configured None means server-default (often tiny), not unlimited.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-15T21:55:00+00:00 — agent-ic204
**Context:** IC-204 provider registry + role routing, phase-2 wave.
**Changed:** ironcore/providers/registry.py (new), tests/providers/test_registry.py (11 tests), providers/__init__.py (additive ProviderRegistry export).
**Verified:** uv run --extra dev pytest -q -> 139 at run time (isolated +11); ruff clean.
**Next:** IC-502 boots via ProviderRegistry.from_settings(settings) and MUST await registry.close_all() at shutdown. IC-801: no mutation API — a model switch builds a NEW registry (default is built eagerly; cache is keyed by MODEL not role).
**Gotchas:** close_all is async + idempotent; post-close for_role raises RuntimeError. VALID_ROLES static tuple pinned against RoleModels.model_fields by a test. Factory convention: all-keyword call, transport omitted when None.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-15T21:55:00+00:00 — agent-ic205
**Context:** IC-205 endpoint capability detection, phase-2 wave.
**Changed:** ironcore/providers/detect.py (new: EndpointFeatures, detect, as_priors, PRIOR_SCORE=0.5), tests/providers/test_detect.py (20 tests). Intentionally NOT exported from providers/__init__ — import ironcore.providers.detect directly.
**Verified:** uv run --extra dev pytest -q -> 180 passed; ruff clean.
**Next:** IC-601/IC-603 OVERWRITE prior keys with measured scores (priors are accept-signals, not reliabilities; all 0.5, below every ladder threshold so unprobed models stay on the text floor — test-pinned against a real CapabilityProfile).
**Gotchas:** all-False + hint unknown = dead-endpoint signature (do not cache); grammar/guided_json trustworthy only on their matching server hints; logprobs is the only body-verified feature; detect() never raises and emits no messages (no key-leak surface).
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-15T22:40:00+00:00 — validator-round
**Context:** Adversarial validation of phases 1-2 (IC-101..104, IC-201..205) + live proof testing, per the goal "validate everything was done correctly with proof testing".
**Changed:** 7 validator findings fixed: [1-BLOCKER] non-string tool arguments crashed complete() -> re-serialized as repairable data (openai_compat); [2] concurrent audit appends lost records on Windows -> per-path thread lock + msvcrt/fcntl OS lock (audit.py, still stdlib-only); [3] timeout reason mock/real divergence -> ProviderTimeout subclass, stream maps to reason "timeout", mock raises the same subclass; [4] closed registry handed out closed providers -> _ensure_open on default/for_role; [5] OllamaProvider bare-root URL half-broken -> auto-appends /v1; [6] non-ConfigError leaks from Settings.load -> ValidationError wrapped, garbage-section env guard; [7] MockProvider never streamed usage -> parity event. Added tests/test_validator_regressions.py (9 pins, one per finding) + tests/test_e2e_live_server.py (6 real-socket proofs: stdlib HTTP server on 127.0.0.1 driven by the real providers — SSE fragmented tool-call reassembly, 429 Retry-After retry, Ollama discovery/show/num_ctx warning, detect() hint+priors, dead-endpoint key-redaction).
**Verified:** uv run --extra dev pytest -q -> 195 passed; ruff clean. Validator's clean-checks: stream done|error termination on 8 adversarial paths, retry semantics exact (N+1 attempts, Retry-After capped 30s, 400 never retried), key redaction across full exception chains, dependency rules intact.
**Next:** Phase 3 (IC-301..304 tools) is unblocked; IC-403/502 write audit via the typed helpers; engine consumes ProviderTimeout distinction for budget-aware retry decisions.
**Gotchas:** audit locking uses byte-0 msvcrt region locks on Windows (LK_LOCK retries ~10s then OSError — fine for tiny writes); OllamaProvider now ALWAYS chats under /v1 even for bare-root config; garbage config sections make env overrides inert for that section (fail-loud at validation, not silent-fix).
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-15T23:40:00+00:00 — wave1-phase3-4
**Context:** Phases 3+4 wave 1 — 8 parallel one-pass agents: IC-401 jail, IC-402 command policy, IC-403 approvals, IC-404 redaction, IC-405 snapshots, IC-406 injection, IC-301 read tools, IC-303 shell.
**Changed:** NEW ironcore/safety/{jail,commands,redact,snapshots,injection}.py, ironcore/core/approvals.py, ironcore/tools/{fs_read,shell}.py + their tests; policy.py DENYLIST_SEED extended + RISKY_PATTERN_SEED added (both non-frozen per CONTRACTS §1). safety/__init__.py and tools/__init__.py deliberately untouched (IC-304 owns tool re-exports) — consumers import submodules directly for now.
**Verified:** uv run --extra dev pytest -> 671 passed; ruff clean.
**Next:** IC-302 (write tools + patcher) consumes jail.resolve_jailed (use the RETURN value, real resolved path). IC-304 assembles fs_read + shell + write tools + a fetch tool into build_default_registry(settings, workspace). IC-502 engine: classify_command before EXEC, downgrade_for_flag(detect_injection(wrapped)) on next gate, redact_context before provider send, ApprovalBroker at GATE, snapshot() each mutating turn.
**Gotchas:** jail — open the returned path, not the input (symlink/Win32 defenses void otherwise). command policy — classify_command composes the EXEC gate internally (don't also call decide); "format " seed matches `git log --format` (surface matched rule in transcript). redaction — pattern-only until boot calls set_default_redactor(from_env); redact composed text not fragments. snapshots — first snapshot() makes `?? .ironcore/` appear; undo() auto-banks dirty state; constructor raises if git missing (construct lazily). injection — preamble is once-per-session (engine adds to system prompt), downgrade takes base decision. shell — branch on data["timed_out"] not exit code.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T00:10:00+00:00 — wave2-ic302
**Context:** Phase 3 wave 2 — IC-302 deterministic patcher + write tools (needed jail from IC-401 + read patterns from IC-301).
**Changed:** NEW ironcore/tools/patch.py (pure appliers), ironcore/tools/fs_write.py (WriteFileTool/EditFileTool), tests/tools/test_patch.py (54 tests).
**Verified:** uv run --extra dev pytest -> 725 passed; ruff clean.
**Next:** IC-304 assembles fs_read + shell + fs_write (+ a fetch tool) into build_default_registry(settings, workspace) and owns tools/__init__.py re-exports.
**Gotchas:** EditFileTool `edit` arg carries the payload for ALL formats (diff text / marker text / full content); `format` enum = EDIT_FORMATS in ladder order. PatchResult.reason is already model-facing (IC-503 passes it through verbatim). no_op success is ok=True + data["no_op"]=True (not failure, not progress). Every path jailed before any fs contact; atomic os.replace; binary/non-UTF-8 refused not corrupted.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T00:35:00+00:00 — wave3-ic304
**Context:** Phase 3 wave 3 — IC-304 default toolset assembly (needed all of 301/302/303).
**Changed:** NEW ironcore/tools/default.py (build_default_registry), ironcore/tools/fetch.py (FetchUrlTool NET — scoped addition, ledger Files reconciled), tests/tools/test_default.py (14 tests), tools/__init__.py (additive exports of all 8 tool classes + build_default_registry).
**Verified:** uv run --extra dev pytest -> 739 passed; ruff clean.
**Next:** IC-502 engine builds the registry at boot (per-session workspace = per-session registry; no dynamic add/remove — config flip rebuilds). Phase 3+4 COMPLETE.
**Gotchas:** roster is 7 local tools (read_file/list_dir/glob/grep/write_file/edit_file/shell) always + fetch_url only when settings.safety.network_tools. FetchUrlTool is the ONLY tool with no workspace ctor arg (transport= seam for tests) — build via build_default_registry, not generic cls(workspace). apply_patch is NOT a registered tool (patch.py appliers are harness-side, consumed by edit_file). registry.all() preserves registration order for display.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T01:15:00+00:00 — validator-round-phase34
**Context:** Adversarial validation of phases 3+4 (IC-301..304, IC-401..406) + real end-to-end proof testing, per the goal "validated proof of outcome + updated readme".
**Changed:** 1 BLOCKER fixed — ReDoS in redact.py PEM pattern (naive `.*?` was O(n^2) with many unclosed BEGIN markers: 1MB=35s, violating IC-404's own <100ms/1MB criterion on a security choke point). Fix = tempered lazy token `(?:(?!-----BEGIN )[\s\S])*?` → linear (700KB now 0.01s), pinned by tests/test_redact.py::test_many_unclosed_pem_markers_do_not_redos. Added tests/test_e2e_phase34.py (13 real-outcome proofs: write→read→edit→run→grep through the actual tools + real python subprocess, byte-identical-on-edit-failure, CRLF preservation, jail escape refusal, command-policy denials incl obfuscation, real process-tree kill on timeout, redaction of a realistic payload, injection nonce breakout resistance, real-git snapshot undo/redo byte-exact + user-repo transparency, registry roster/network gating). README roadmap flipped phases 3+4 to shipped.
**Verified:** uv run --extra dev pytest -> 753 passed; ruff clean. Validator clean-checks: jail (symlink-out/drive-relative/NUL all reject, returns realpath), write-tools-jailed + byte-identical-on-failure + atomic, patcher fuzz/ambiguity/CRLF, shell tree-kill, command-policy 11 bypass variants DENY + tighten-only + ceiling, snapshots byte-exact + user-transparent (git + non-git), injection nonce + corpus, approvals timeout/grants/race, assembly roster + fetch scheme/timeout, safety stdlib-only, no tool self-gates/prints.
**Next:** Phase 5 (turn engine IC-501..506) — IMPORTANT non-phase-3/4 note from the validator: READ tools deliberately don't jail (resolve absolute paths untouched); since READ is `allow` in every mode, IC-502 MUST add the engine-side "reads outside workspace ASK" gate (SAFETY T4) or read_file could slurp ~/.ssh/id_rsa.
**Gotchas:** the redact perf guarantee is now pinned by a pathological many-BEGIN case, not just benign 1MB (that test masked the bug).
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T02:30:00+00:00 — wave1-phase5-6
**Context:** Phases 5+6 wave 1 — 4 parallel standalone modules: IC-501 composer, IC-606 ironcall, IC-607 sampling, IC-601 probe runner.
**Changed:** NEW ironcore/core/{composer,ironcall,sampling}.py, ironcore/envelope/runner.py + tests. No engine.py, no __init__ edits.
**Verified:** uv run --extra dev pytest -> 836 passed; ruff clean.
**Next:** IC-502 engine consumes all four. Probe interface for IC-602/603/604: Probe protocol {id,title,targets:Sequence[str],async run(provider)->ProbeResult{probe_id,scores:dict[dotted-path->float],notes,ok}}; run_probes merges dotted paths (tool_protocols.<name>/edit_formats.<name>/honest_context/json_adherence/instruction_retention/coherence_horizon), degrades reliability targets to 0.0 on raise/ok=False, context left at base. STAMP probed_at yourself (no datetime.now in module).
**Gotchas:** composer — anchor is a SEPARATE system message (engine merges if provider only honors first system msg); working_set param is dict[relpath->text] MRU-first (distinct from state.working_set list); response headroom 15% reserved → set max_tokens from it; redact_context already applied to working-set+history (don't double-redact). ironcall — parse() returns AT MOST ONE call (text protocol = one call/turn; loop by feeding render_result back); IroncallParse.error is model-facing for repair, warning is dimmed-not-fatal. sampling — resolve_sampling(kind in tool|edit|plan|brainstorm, attempt) raises ValueError on other kinds; best_of budget duck-type = should_continue()|remaining()|callable.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T03:30:00+00:00 — wave2-4-engine-probes
**Context:** Phase 5 engine (IC-502+605) + phase 6 probes (IC-602/603/604), run concurrently (core/ vs envelope/).
**Changed:** NEW ironcore/core/protocols.py (4 collaborator Protocols + Default impls), REPLACED ironcore/core/engine.py (full TurnEngine), NEW ironcore/envelope/probe_{ctx,tools,edits}.py + tests. events.py/composer/etc untouched.
**Verified:** uv run --extra dev pytest -> 907 passed; ruff clean.
**Next (Wave 3 — implement the FULL collaborators, drop-in for protocols.py, inject into TurnEngine, own new files only):**
  IC-503 core/repair.py — RepairPolicy.decide(*, attempt, error, raw, rung)->RepairAction(RETRY/LADDER_DOWN/GIVE_UP); full ladder (retry once → LADDER_DOWN → GIVE_UP, bounded). Engine ALREADY handles all 3 actions.
  IC-504 core/verify.py — Verifier.verify(workspace, settings, state, touched_files)->VerifyResult(ok, summary, ran); command discovery (IRONCORE.md/config/auto-detect pytest|npm test|cargo test) + subprocess run. Engine surfaces summary; the "feed-failures-back-once" re-loop is an engine edit the ORCHESTRATOR will add after (don't edit engine.py).
  IC-505 core/steps.py + core/compact.py — StepPlanner.advance(state, evidence)/is_complete(state) (evidence-gated) + compaction→handoff-grade summary (summarizer via provider, mechanical fallback). Compaction TRIGGER wiring is an orchestrator engine edit (don't edit engine.py).
  IC-506 core/budgets.py — BudgetTracker.start_turn()/record_call(tokens)/check()->str|None/note_tool(name,args)->str|None/should_continue()->bool; caps calls+tokens+wallclock(time.monotonic internal)+repairs + loop detection. stop_reason string "budget".
**Gotchas:** engine emits repair/verify notes as TextDelta prefixed [repair]/[verify] (events.py frozen, no note event). unprobed profile → recommended_tool_protocol()=="text_protocol" (tests needing native pass tool_protocols={"native":1.0}). constructor: TurnEngine(provider,tools,settings,profile,mode=MANUAL,*,workspace=REQUIRED,approvals,snapshots,repair,verifier,budget,planner,session,system_prompt). Probes: import from ironcore.envelope.probe_* directly (__init__ not touched); notes not persisted (read from live evaluate_probes for /envelope).
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T04:45:00+00:00 — wave3-collaborators+integration
**Context:** Phase 5 wave 3 — the four full collaborators (IC-503 repair, IC-504 verify, IC-505 steps+compact, IC-506 budgets) + orchestrator engine integration.
**Changed:** NEW ironcore/core/{repair,verify,steps,compact,budgets}.py + tests. Orchestrator engine.py edits: swapped defaults to LadderRepairPolicy/CommandVerifier/Budget/PlanStepPlanner; wired the SPEC §5.5 verify feed-failures-back-once re-loop (verify now runs at the clean stop, feeds back once, surfaces on the second stop); wired the SPEC §11.2 compaction trigger (should_compact→compact at loop top, keep _KEEP_RECENT=6 tail); enriched the repair re-ask with frame_error. Updated test_verify.py's engine-integration test to script the corrective round.
**Verified:** uv run --extra dev pytest -> 975 passed; ruff clean.
**Next:** Phase 5+6 feature-complete. Deferred polish (non-blocking, reported by agents): step-wise LADDER_DOWN (engine jumps straight to text floor — behaviorally identical today since strict_json rides the native path); budget.note_repair() hook + budget.summary()→state.budgets_spent (repair cap already enforced by the RepairPolicy GIVE_UP). Phase 7 TUI (IC-701..706) consumes the engine's event stream.
**Gotchas:** CommandVerifier is now the DEFAULT verifier — in a real project it auto-detects+runs pytest/npm/cargo on write turns; in tmp-workspace tests (no markers) it discovers nothing → ok=True (harmless). Compaction fires only when history estimate exceeds honest_context*HISTORY_SHARE — small test turns never trigger it. The verify feed-back-once consumes an extra provider completion per failing-verify turn (script accordingly in engine tests).
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T05:30:00+00:00 — validator-round-phase56
**Context:** Adversarial validation of phases 5+6 (IC-501..506/601..607) + real end-to-end proof testing + README, per the goal.
**Changed:** 2 BLOCKERS fixed (both in the compaction path the orchestrator wired): [1] compact.py sent the UNREDACTED transcript to the provider (bypassed the composer's IC-404 choke point → secret exfil on hosted endpoints, SAFETY T4) → redact_context in _render_transcript + mechanical digest; [2] engine reported stop_reason="done" while verify was still failing (SAFETY T7 / SPEC §5.5 "cannot report unverified as done") → new "goal-unmet" stop_reason after the fed-back-once verify still fails. Plus [3-MAJOR] compaction was unbudgeted + re-fired every OBSERVE iteration → once-per-turn guard + budget.record_call; [4-MINOR] verify now runs on did_mutate (EXEC too, not just WRITE). Added tests/test_validator_regressions_p56.py (3 pins) + tests/test_e2e_phase56.py (10 real-engine proofs: full read→edit→run turn, PLAN-deny/MANUAL-approve/AUTO-rm-rf-deny gating, budget runaway stop, repair recovery, IRONCALL floor executes a tool, secret redacted before the model, envelope measure→adapt feeding the engine). README roadmap flipped phases 5+6 to shipped.
**Verified:** uv run --extra dev pytest -> 989 passed; ruff clean; branding grep clean.
**Next:** Phase 7 TUI (IC-701..706) — a THIN Textual client over the engine's core.events stream + ApprovalBroker.answer(); the engine already emits everything it needs. Deferred non-blockers: step-wise LADDER_DOWN, budget.summary()→state.budgets_spent, read-gate hardcodes arg name "path" (fine for all 4 current READ tools).
**Gotchas:** compaction now counts as a provider call (budget) + fires once/turn; verify runs on any mutation and a persistent failure yields stop_reason "goal-unmet" (headless exit-code mapping per SPEC §9 should treat goal-unmet as non-zero). The verify feed-back-once consumes an extra completion per failing-verify turn — script engine tests accordingly.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T07:00:00+00:00 — wave1-phase7-8
**Context:** Phases 7+8 wave 1 — IC-1001 session store + IC-701..704 interactive Textual TUI.
**Changed:** NEW ironcore/memory/sessions.py (SessionStore), ironcore/tui/{app.py, widgets/*, screens/approval.py}; cli.py TTY-gated TUI launch. + tests.
**Verified:** uv run --extra dev pytest -> 1020 passed; ruff clean; ironcore --version/doctor OK.
**Next (Wave 2, parallel — tui/ vs commands/, no collision):**
  IC-705+706 (tui/): diff viewer widget → plug into ApprovalScreen #approval-preview (keyed on request.risk) + ToolCard; SessionPicker ModalScreen + cli.py --resume flag threading a session id into IronCoreApp.from_settings → TurnEngine(session=). App already installs engine.approvals.on_request at mount; ApprovalRequest.preview already carries the exact effect.
  IC-801..807 (commands/): phase-8 handlers reading ctx.extra = {app, engine, registry, workspace, provider_registry, settings, schedule}. schedule(coro)->None runs a Textual worker + posts the coro's str result to the transcript (this is how async commands like /model list, /review, /goal-check return without blocking). Handlers stay SYNC returning str; long work via schedule(). Own commands/ + builtins.py.
**Gotchas:** cli.py launches the TUI only when sys.stdout.isatty() (non-TTY prints the banner — keeps the suite from hanging). Approval modal maps y/n/a→ApprovalAnswer(approve once / deny / approve turn), Deny focused for exec/net. Session ids are caller-stamped (no datetime.now in the store) — TUI/commands must supply the timestamp+id. rehydrate returns (list[Message], tail_summary_str).
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T08:30:00+00:00 — wave2-phase7-8
**Context:** Phases 7+8 wave 2 — IC-705 diff viewer + IC-706 session picker/resume + IC-801..807 all phase-8 commands.
**Changed:** NEW tui/widgets/diffview.py, tui/screens/sessions.py; MOD tui/screens/approval.py + widgets/transcript.py + app.py (session recording/resume) + cli.py (--resume). NEW commands/{_helpers,modelcmd,initcmd,goalcmd,loopcmd,lifecyclecmd,reviewcmd,memorycmd}.py; MOD commands/builtins.py (registers all real handlers; only /workflow /envelope /probe remain planned). + tests.
**Verified:** uv run --extra dev pytest -> 1087 passed; ruff clean; ironcore --version/doctor OK. Phases 7+8 feature-complete.
**Next:** Phase 9 workflows (IC-901..905), phase 10 memory/sessions wiring (IC-1002 handoff lifecycle, IC-1003 IRONCORE.md injection), phase 11 packaging/v0.1. Still-planned commands /envelope /probe = IC-608, /workflow = IC-904.
**Gotchas:** async commands (/model list, /compact, /review, /goal check) return an ack then post the real result via ctx.extra["schedule"](coro); the coro returns the str. /model switch doesn't rebuild ProviderRegistry (no live-mutation API) — updates settings + advises /probe. /loop uses optional app.register_loop/stop_loop hooks (hasattr-guarded); to drive recurrence, IronCoreApp needs those two methods (LoopSpec(prompt, interval_s), interval_s=None=self-paced). /goal checks persist in a module-level map keyed by workspace (TUI rebuilds ctx per dispatch). App records user line in _start_turn, assistant in _drive_turn finally (partial-on-Esc preserved); slash commands not recorded. --resume threads into engine._conversation for continuity; TTY-gated.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T09:30:00+00:00 — validator-round-phase78
**Context:** Adversarial validation of phases 7+8 (IC-701..706, IC-801..807, IC-1001) + real end-to-end proof + README.
**Changed:** Validator SHIP verdict, no blockers; 2 MAJORs fixed: [1] /help crashed on empty ctx.extra (bare subscript) → ctx.extra.get("registry") or build_default_registry(); [2] `ironcore --resume <bad-id>` fabricated a headerless orphan session + misreported "resumed" → guard in _resume_session (path_for exists check → start fresh with a note). Added tests/test_validator_regressions_p78.py (2 pins) + tests/test_e2e_phase78.py (10 proofs: real TUI session stream/record/mode-cycle, approval-deny prevents a write, /init→CommandVerifier round-trip, /undo restores a real git edit, /goal sets engine anchor, every command survives a thin context, from_settings constructs). README flipped 7+8 to shipped + Beta-stage framing + TUI launch command.
**Verified:** uv run --extra dev pytest -> 1099 passed; ruff clean; branding grep clean; ironcore --version/doctor OK.
**Next:** Phase 9 workflows (IC-901..905), phase 10 IC-1002 handoff-lifecycle + IC-1003 IRONCORE.md injection, phase 11 packaging/CI-hardening/v0.1 (IC-1101..1104). Deferred MINORs (validator, non-blocking): goalcmd._VERIFY_CHECKS/loopcmd._LOOPS are process-global (one workspace/process = fine); TUI + /compact read engine._conversation (documented IC-706 seam).
**Gotchas:** /help now falls back to a fresh registry when ctx.extra lacks "registry"; --resume of a missing id starts a fresh listable session (no orphan). Validator-confirmed clean: approval Deny-default for exec/net, deny prevents execution, thin-client (only cli.py imports tui), session-id traversal rejected, secrets never persisted to the session JSONL.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T11:00:00+00:00 — wave1-phase9-11
**Context:** Phases 9-11 wave 1 — IC-901 subagent runner, IC-902 workflow schema, IC-1002 handoff lifecycle, IC-1003 memory injection, IC-1101 CI hardening. Orchestrator added pyyaml+pytest-cov to pyproject + wired load_project_memory into the engine.
**Changed:** NEW workflows/subagent.py, workflows/schema.py; MOD memory/handoff.py + core/engine.py (handoff lifecycle + memory-load wiring), core/composer.py (load_project_memory), docs/CONTRACTS.md §9 (frozen), .github/workflows/ci.yml (coverage gate), pyproject.toml (pyyaml/pytest-cov/coverage config), uv.lock. + tests.
**Verified:** uv run --extra dev pytest -> 1163 passed; ruff clean; ci.yml valid YAML; coverage 91-94% > 85% gate.
**Next (Wave 2):** IC-903 workflows/engine.py orchestrator (WorkflowRunner: execute phases fanout/foreach/reduce via run_subagent per item, concurrency cap, per-agent failure isolation→null, progress events); IC-1103 demo/ + test_demo.py (scripted MockProvider session, python -m demo); IC-1102 .github/workflows/release.yml (tag→PyPI trusted publishing). Then Wave 3: IC-904 /workflow cmd+progress UI, IC-905 built-in YAML workflows. Then IC-1104 v0.1 (CHANGELOG+README+ready-to-tag; do NOT actually publish to PyPI without the user).
**Gotchas:** subagent run_subagent: task.mode authoritative (overwrites engine.mode); no-schema clean stop = ok=True (doesn't inspect stop_reason); raw exceptions NOT caught (IC-903 wraps per-item for fan-out isolation); validate_against is a JSON-schema SUBSET (type/required/properties). schema: output_schema is an inline dict NOT a string; foreach agent is a SIBLING field + {{...}} ref; discover_workflows keys by filename stem; interpolate is string-only (resolve raw lists yourself). handoff: call engine.end_session() ONCE on quit (append-only). memory: load_project_memory re-read per turn (mtime-cached summarize path); summarizer is sync but engine's is async — left as truncation for now.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T12:15:00+00:00 — wave2-phase9-11
**Context:** Phases 9-11 wave 2 — IC-903 workflow orchestrator, IC-1103 offline demo, IC-1102 release automation.
**Changed:** MOD workflows/engine.py (WorkflowRunner full impl); NEW demo/{__init__,__main__,scenario}.py; NEW .github/workflows/release.yml. + tests.
**Verified:** uv run --extra dev pytest -> 1186 passed; ruff clean; `python -m demo` exits 0 with a narrated real session; release.yml dry-run built ironcore-0.1.0-py3-none-any.whl that installs + runs `ironcore --version`.
**Next (Wave 3):** IC-904 /workflow command (commands/workflowcmd.py + builtins.py register, drop stub) + progress UI (tui/widgets/workflowview.py + app wiring) — driven by WorkflowRunner via ctx.extra["schedule"], renders WorkflowProgress; IC-905 built-in workflows (workflows/builtin/{review,migrate,explain-repo}.yaml + test). Then IC-1104 v0.1 (CHANGELOG+README; ready-to-tag, do NOT publish without user).
**Gotchas:** WorkflowRunner.run() NEVER raises for content/structural errors (returns WorkflowResult ok=False + notes); load_workflow raises WorkflowError at load. item_done fires in COMPLETION order (use .index). Built-in YAML: foreach ref must resolve to a LIST already in context (a fanout stores a list under context[phase.id]; {{find.findings}} only works if the item is a dict w/ findings key); per-item context adds {{item}}; reduce reduces the IMMEDIATELY-prior phase; reducers = count/concat/list/markdown_table or {op:...}; give fanout agents output_schema for dict outputs. Demo: engine emits no event on verify PASS (narrator reads recorded VerifyResult). Release: publish needs one-time PyPI trusted-publisher + a `pypi` GH environment (documented in release.yml header); no token embedded.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T13:30:00+00:00 — validator-round-phase9-11 + v0.1
**Context:** Adversarial validation of phases 9-11 + real proof + IC-1104 v0.1 release finalization. IronCore is now feature-complete.
**Changed:** Validator FIX-FIRST → clean: 1 BLOCKER fixed (workflow subagents ignored the session mode → a PLAN session could mutate through a workflow; SAFETY T8) — WorkflowRunner gained a `mode` param threaded into SubagentTask, /workflow sources it from engine.mode; + 2 MAJORs (binary IRONCORE.md crashed turns → composer read_text errors="replace"; subagents stalled 300s on ASK gates → /workflow subagents get an unattended fast-deny broker). Added tests/test_validator_regressions_p911.py (3 pins) + tests/test_e2e_phase9_11.py (9 proofs: shipped built-in workflow runs, failure isolation, /workflow gates first-run, compaction handoff w/o secret leak, memory injection budgeted). IC-1104: CHANGELOG.md + README flipped to v0.1 feature-complete + roadmap all-shipped. Wheel ships the 3 built-in workflow YAMLs (verified).
**Verified:** uv run --extra dev pytest -> 1223 passed; ruff clean; coverage 93.95% > 85% gate; ironcore --version OK; branding grep clean; `uv build` → pure-py wheel installs + runs.
**Next:** v0.1 is READY-TO-TAG. To cut the release: the maintainer configures PyPI trusted publishing (Owner RealDealCPA-VR, Repo IronCore, Workflow release.yml, Environment `pypi`) + a `pypi` GH environment, then pushes tag v0.1.0 → release.yml builds/verifies/publishes. NOT auto-published (no credentials/user intent). All 45 ledger tasks IC-001..IC-1104 are [x] done.
**Gotchas:** workflow subagents now inherit the session mode (PLAN can't mutate; MANUAL/ACCEPT_EDITS auto-deny gated actions fast, don't stall). load_project_memory is binary-safe (errors="replace"). dist/ is gitignored (don't commit wheels).
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T15:00:00+00:00 — post-v0.1 review: molds-to-model wiring + README
**Context:** Deep code review of the "molds itself to the local model" core promise + README rewrite (roadmap removed, moonshots added).
**Changed:** Review (me + an independent reviewer agent, execution-verified) found the envelope machinery was BUILT but NOT WIRED at runtime: from_settings used a floor default and nothing ran the probe suite; recommended_edit_format was measured-but-dead; Ollama's keep_alive/introspection unused. Fixed: NEW envelope/suite.py (probe_model + default_probe_suite), NEW commands/envelopecmd.py (/probe hot-swaps engine.profile + /envelope report card — IC-608 now real, zero remaining stubs), app first-use background auto-probe (envelope.auto_probe default on) + from_settings wiring, engine _system_prompt now steers recommended_edit_format, registry select_provider_factory builds OllamaProvider for :11434 endpoints (keep_alive), doctor reports probe status. Config: provider.type + envelope.auto_probe. tests/test_envelope_wiring.py (8 pins). README fully rewritten (banger+factual, roadmap table REMOVED, 🌙 Moonshots section added from the review's deeper-adaptivity findings).
**Verified:** uv run --extra dev pytest -> 1229 passed; ruff clean; coverage 94%; demo runs; doctor shows envelope status; branding clean.
**Next / MOONSHOTS (documented in README, honest future work from the review):** instant-on profiling (seed from Ollama /api/show + detect() priors, then deepen in bg); guided-decoding for a real strict_json rung (Provider needs a response_format/grammar seam — today engine only branches text_protocol vs not); per-role model routing w/ a profile per role (engine always uses provider_registry.default); live /model re-point + re-probe mid-session (provider bound once today); model-aware tokenizer (replace chars/4); best-of-n resampling (harness exists, unwired); plugin/entry-point extensibility; vision/MCP/self-tuning ladders.
**Gotchas:** OllamaProvider is now the DEFAULT for :11434 (subclass of OpenAICompatProvider, isinstance-compatible; adds keep_alive to chat bodies). auto-probe only fires via from_settings (injected-engine tests unaffected; gated by auto_probe ctor flag). probe_model DEGRADES a dead endpoint to floor (never raises) — honest but caches a floor profile; re-/probe when the endpoint is back.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T17:00:00+00:00 — instant-on-profiling (swarm)
**Context:** Moonshot shipped via a doer→validator swarm (plan: docs/plans/instant-on-profiling.md). Seed a usable profile in ~1s from endpoint introspection, then deepen in the background — no cold-probe wait.
**Changed:** NEW envelope/seed.py (seed_profile: Ollama /api/show context + detect() capabilities → provisional native/search_replace + real window; never raises, never cached). CapabilityProfile.source field (default/seeded/probed; CONTRACTS §5 additive). suite.probe_model + envelopecmd.probe_and_swap gained base= so the deep probe REFINES the seed (introspected context survives a probe failure). runner.run_probes stamps source="probed" + render_report_card shows source honestly. app _mold_to_model: seed (hot-swap #1) then probe (hot-swap #2), guarded to endpoint providers + try/except. config EnvelopeSettings.instant_seed (default True). doctor honest wording. tests/test_envelope_seed.py + tests/test_e2e_instant_on.py (real seed vs MockTransport Ollama).
**Verified:** VALIDATOR-1 (seed core) SHIP + VALIDATOR-2 (wiring) SHIP, both execution-verified. uv run --extra dev pytest -> 1250 passed; ruff clean; coverage 94%; doctor honest; branding clean.
**Gotchas:** seed is PROVISIONAL — probed_at stays None so the deep probe still fires; only the measured profile is cached. honest_context = min(num_ctx, window) (never overruns the server). seed skipped for providers without base_url (bare MockProvider). A seeded profile is "provisional" NOT "measured" in the card/verdict (source distinguishes it from probed + default). Swarm: DOER-1→VALIDATOR-1 (seed core), then DOER-2A(app)‖DOER-2B(config/display)→VALIDATOR-2, then orchestrator e2e proof.
**Next MOONSHOTS (README):** guided-decoding strict_json rung, per-role envelopes, live /model re-point, model-aware tokenizer, best-of-n, plugins, vision/MCP.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T19:30:00+00:00 — guided-decoding (swarm)
**Context:** Moonshot shipped via a doer→validator swarm (plan: docs/plans/guided-decoding.md). Make the strict_json rung REAL server-side constrained decoding.
**Changed:** Provider seam — additive `response_format` + `extra_body` kwargs on complete/stream (base/openai_compat/ollama/mock; CONTRACTS §2; extra_body wins clashes; Mock records last_*). NEW core/guided.py — tool_call_response_format (json_schema pinning {tool:enum-of-names+done, args:object}), render_json_system_fragment (catalog + done + 2 examples), parse_guided_tool_call → GuidedParse(call/done/message/error, exclusive 3-way, never raises). engine strict_json path (is_guided/text_frame): guided fragment in system prompt, response_format on stream, NO native tools, suppressed raw-JSON TextDelta, parse→done(show message)/call(execute)/error(repair), ladder-down→text floor. probe_tools ToolFormProbe strict_json trials now send response_format (measures GUIDED reliability). 3 stream doubles updated for the seam. tests/test_e2e_guided.py.
**Verified:** VALIDATOR-1 (seam+helper) SHIP + VALIDATOR-2 (engine path) SHIP, both execution-verified (51 + assertions). uv run --extra dev pytest -> 1294 passed; ruff clean; coverage 94%; branding clean.
**Gotchas:** guided suppresses TextDelta for the JSON scaffold (only done-message + tool cards shown). `done` pseudo-tool lets a constrained model finish (schema forces JSON every turn). result feedback reuses the text-floor path. gate/budget/verify/snapshot all preserved (guided is just a 3rd parse shape). extra_body is the GBNF/vLLM guided_json escape hatch. LADDER_DOWN from strict_json → text_protocol (IRONCALL) automatically. probe: MockProvider.last_response_format is last-only; strict_json trials run before text (which clears it) — assert on the sequence.
**Next MOONSHOTS (README):** per-role envelopes, live /model re-point, model-aware tokenizer, best-of-n, plugins, vision/MCP.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T21:30:00+00:00 — fable-ms1
**Context:** MS-1 moonshot — model-aware tokenization: replace the universal chars/4 estimate with a per-model MEASURED chars-per-token ratio, consumed by the composer and the compaction predicate.
**Changed:** NEW envelope/probe_ratio.py (TokenRatioProbe: known-char filler docs vs server-reported prompt_tokens/input_tokens; ratio clamped [1.0, 8.0]; no-usage -> ok=True with EMPTY scores so the 4.0 base survives honestly). CapabilityProfile.chars_per_token (float, default 4.0, additive — CONTRACTS §5 + migration note, same commit). runner: chars_per_token in _FLOAT_FIELDS but NOT _RELIABILITY_ROOTS (a failed measurement keeps the base, never 0.0); report card gains a "Token ratio: X.X chars/token" line ("(default)" suffix at 4.0 — NOT "(unmeasured)", the provenance pin forbids the substring "measured" pre-verdict). probes.py TOKEN-RATIO ProbeSpec; suite default_probe_suite is now SEVEN probes. composer: estimate_tokens(text, chars_per_token=4.0) with the exact-integer (len+3)//4 fast path at 4.0 and math.ceil otherwise; _safe_ratio guards <=0/NaN/inf at consumption; cpt threaded through _build_system/_build_working_set/_select_history/_truncate_to_tokens (keep_chars = int(tokens * ratio), trim-back loop guarantees the invariant at ANY ratio)/load_project_memory; compose reads profile.chars_per_token. compact.should_compact estimates at the profile ratio. docs/MODELS.md §2 table row. README: Moonshots section gained a "**Shipped:**" lead-in with the flipped bullet. TODO.md Moonshots ledger + this block. Tests: NEW tests/test_probe_ratio.py (11) + additions to test_composer/test_compact/test_probe_runner/test_envelope/test_report_card (28 total).
**Verified:** `uv run --extra dev pytest` -> 1322 passed in 21.43s (baseline 1294 + 28). `uv run --extra dev ruff check .` -> All checks passed! Proofs: estimate_tokens('x'*400 | ,2.0 | ,0) -> 100 200 100; run_probes(usage prompt_tokens=2000 x3) -> source=probed, chars_per_token=4.21; save/load round-trips 3.2; a legacy envelope JSON WITHOUT the field loads as 4.0; suite lists 7 probe ids; card renders "Token ratio: 3.2 chars/token".
**Next:** MS-2/MS-5 fixture authors: default_probe_suite() is now SEVEN probes — monkeypatch the 1-trial suite (tests/test_envelope_wiring.py:93 pattern) instead of scripting exact MockProvider entry counts against the default battery; MS-5 plugin probe ids must not collide with "TOKEN-RATIO". MS-3 (per-role profiles) and MS-6 (image budget) get ratio-awareness free by passing profile= / the same cpt into the estimator; MS-6 must charge IMAGE_TOKEN_COST in _select_history via the ratio-aware estimator at the SAME cpt and keep the budget invariant tests true under images.
**Gotchas:** chars_per_token is NOT a reliability — degrade paths leave it at base (asserting 0.0 after failure is wrong). CTX-HONESTY "sizes" are vocab-word counts, not real tokens, so honest_context and chars_per_token are measured on different bases — a small measured ratio errs conservative (packs FEWER chars), never overflows. estimate_tokens ratio 4.0 (and any invalid value) is byte-identical legacy math — every pre-MS-1 test/number unchanged. Report-card tests stay substring/isascii-based, never exact-snapshot (MS-8 'tuned' + MS-6 Vision lines will append). Servers that omit usage (some llama.cpp configs) honestly keep 4.0 — designed-for, not a bug.
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-16T23:30:00+00:00 — fable-ms2
**Context:** MS-2 moonshot — live model swaps: `/model <name>` re-points the RUNNING provider mid-session, hot-swaps the envelope from the on-disk cache (instant on a hit), and seeds + background-deepens on a miss.
**Changed:** providers/registry.py `for_model(model)` (additive; shares the per-model `_build`/`_providers` cache the roles use — swap-back reuses the instance; closed registry raises). core/engine.py `TurnEngine.repoint(provider, profile)` (additive — CONTRACTS §4 bullet, same commit; also refreshes `handoff_author`; deliberately does NOT close the old provider — registry owns lifecycle, MS-8 extends this path). commands/modelcmd.py switch path rewritten: busy guard (duck-typed `app._turn_running()`, no tui import) → same-model no-op → settings mutate → `registry.for_model` (closed/headless ⇒ the IC-801 advisory VERBATIM, wording pinned) → envelope_dir from `ctx.extra` or lazy `default_envelope_dir()` → measured-cache HIT repoints instantly / MISS repoints to floor + schedules `_seed_then_deepen` (seed only for base_url providers; it `schedule()`s `probe_and_swap` as its OWN task so the seed note posts promptly); `_list_models` marks cached-measured models "· measured" (suffix-only). commands/envelopecmd.py `probe_and_swap(..., envelope_dir=None)` (additive kwarg → probe_model; MS-5 must preserve this threading). tui: `StatusBar.set_model`; app.py `_dispatch` re-reads `settings.provider.model` after every dispatch; `envelope_dir` resolved ONCE (`default_envelope_dir()`) in `from_settings`, stored as an app attribute + `ctx.extra["envelope_dir"]` — the single source MS-3 RoleRouter and MS-8 OutcomeLedger must reuse; `_mold_to_model` deepen now threads it too. Tests: 8 new in test_cmd_model.py, 4 in providers/test_registry.py, 1 in test_engine.py, NEW test_model_swap.py e2e (miss→deepen→cache→swap-back pure hit, real ProviderRegistry), 1 TUI status-bar test; two existing `fake_probe(eng)` stubs gained the additive `envelope_dir=None` kwarg (signature-compat with the threaded call).
**Verified:** `uv run --extra dev pytest` -> 1337 passed in 23.65s (baseline 1322 + 15). `uv run --extra dev ruff check .` -> All checks passed! Proof script: cache-hit prints "Switched to 'b' … envelope loaded from cache (measured; …)" with engine.profile.source=="probed" and ZERO queued work; miss prints "… measuring it in the background" with a floor profile active immediately and EXACTLY one queued task.
**Next:** MS-3 (per-role models): consume `app.envelope_dir` / `ctx.extra["envelope_dir"]` for the RoleRouter — do not re-resolve; engine hot-loop is about to become per-role (provider,profile) locals — `repoint` stays the default-role swap seam. MS-5: `probe_and_swap` gains `extra_probes=` NEXT TO `envelope_dir=` (both keyword-only; keep the threading into probe_model).
**Gotchas:** the miss-path drain in sync tests MUST be a queue-and-loop (`while queue: asyncio.run(queue.pop(0))`) — `_seed_then_deepen` schedules the deepen from INSIDE a scheduled coroutine (re-entrant). default_probe_suite is SEVEN probes — tests monkeypatch the 1-trial ToolFormProbe suite (wiring-test pattern) instead of scripting the full battery. MockProvider has no `base_url` attr, so swap-path seeding is skipped for it BY DESIGN (set one and seed_profile will try real endpoint introspection). A stale deepen can still land after a rapid second swap (same exposure the pre-existing background probe_and_swap had — accepted risk, MS-8's swap-path rework is the place to fix it). `_load_measured` treats a corrupt cache JSON as a miss, never a crash. The busy guard only protects TUI callers (headless ctx without "app" can still swap mid-turn).
<!-- HANDOFF v1 END -->

<!-- HANDOFF v1 BEGIN -->
## Handoff — 2026-07-17T01:00:00+00:00 — fable-ms3
**Context:** MS-3 moonshot — a model per role, each measured: the `[roles]` config now routes the TURN LOOP itself, and every routed role runs on its OWN capability envelope from the shared MS-2 cache (planner = PLAN-mode turns, coder = all other turns, summarizer = compaction; verifier stays on the existing `resolve_provider(role="verifier")` seam — CommandVerifier is mechanical, no engine seam).
**Changed:** NEW core/roles.py `RoleRouter(settings, *, registry, envelope_dir, providers, profiles)` — `resolve(role) -> (Provider, CapabilityProfile) | None` (None = "use the caller's primary pair": unset role, identity model, or closed/absent registry — degrade, never crash a turn; unknown role = ValueError parity with the registry); profiles come from a per-model cache accepting only MEASURED disk entries (`source=='probed'` or `probed_at` — the /model cache-hit rule) else floor defaults (CONTRACTS §5); `set_profile` hot-swap; envelope_dir is CALLER-provided (MS-2's single resolution), lazy `default_envelope_dir()` only when omitted. core/engine.py: additive keyword-only `roles=` (CONTRACTS §4 bullet, same commit) + `_binding(role)`; run_turn resolves the ACTIVE `(provider, profile)` locals ONCE per turn and every hot-loop read now uses them — `recommended_tool_protocol`, `should_compact`, `load_project_memory`, `compose`, `resolve_sampling`, `_headroom_tokens(profile)`, `_system_prompt(protocol, profile=...)`, `provider.stream`; compaction calls `self._binding("summarizer")[0]`; `_headroom_tokens`/`_system_prompt` gained optional `profile=` defaulting to `self.profile` (back-compat). providers/mock.py additively records `last_sampling` + `sampling_calls` in complete/stream (CONTRACTS §2 bullet, same commit — MS-4 consumes the list, do NOT re-add). tui/app.py from_settings builds `RoleRouter(settings, registry=provider_registry, envelope_dir=envelope_dir)` → `roles=`. workflowcmd `_factory_from_engine` propagates `roles=getattr(engine, "roles", None)` (subagents inherit routing). lifecyclecmd `_compact_coro` routes via `_summarizer_provider(engine)`. envelopecmd `/envelope` appends a suffix-only roles tail (model, measured vs "unprobed — floor defaults", tools, ctx + the "/model <role-model>" measure hint). docs/MODELS.md §5 per-role envelope paragraph + worked TOML example; README Moonshots bullet flipped to Shipped. Tests: NEW test_roles.py (12) + test_engine_roles.py (6, incl. the 307-max_tokens window proof and closed-registry crash-safety), +1 each in test_cmd_lifecycle/test_envelope_wiring/test_cmd_workflow (21 total).
**Verified:** `uv run --extra dev pytest` -> 1358 passed in 23.31s (baseline 1337 + 21). `uv run --extra dev ruff check .` -> All checks passed! Proof script: a routed coder turn hit coder=1/primary=0 calls with `last_sampling.max_tokens == 307` — 15% of the CODER's 2048-token honest context, not the primary's 8192.
**Next:** MS-4 (best-of-N): generate candidates against MS-3's ACTIVE `(provider, profile)` locals with `resolve_sampling` on the ACTIVE profile; MockProvider.last_sampling/sampling_calls already landed here — consume, don't re-add; the GIVE_UP resample branch slots into the repair block, and the for-call loop → work-queue rewrite must keep `_binding` resolution at turn start. MS-8: record_tool_attempt must record the ACTIVE role's protocol; OutcomeLedger shares the same envelope_dir; the /envelope 'tuned' footer appends AFTER the roles tail (both suffix-only).
**Gotchas:** the ACTIVE pair is resolved ONCE at turn start — a mid-turn `engine.profile` hot-swap (background seed) lands on the NEXT turn for the loop role (pre-MS-3 code re-read self.profile per iteration; between-turn swaps flow through `_binding` unchanged). A routed coder with a smaller window compacts EARLIER (should_compact judges against the ACTIVE window — correct: that is the window being composed into; documented in MODELS.md §5). Role models stay on floor defaults until measured — /model <role-model> once (MS-2 seed→deepen) writes the shared cache; RoleRouter re-reads it lazily per model, and a router-cached FLOOR profile persists for the session (set_profile is the hot-swap seam; background role-seeding is deliberate follow-up scope). The StatusBar still shows only the primary model; /envelope's roles tail is the honest per-role display. RoleRouter profile seam is keyed by MODEL id, provider seam by ROLE name.
<!-- HANDOFF v1 END -->
