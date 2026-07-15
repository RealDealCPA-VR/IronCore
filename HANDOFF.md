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
