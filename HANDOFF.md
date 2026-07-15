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
