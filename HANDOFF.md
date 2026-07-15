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
