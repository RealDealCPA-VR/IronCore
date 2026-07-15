# CONTRACTS.md — frozen interfaces

> These interfaces are load-bearing across parallel work streams. **Changing anything here
> requires editing this file in the same commit as the code change**, with a migration note.
> If your task seems to need a contract change, stop and check whether you're solving the
> right problem.

## How to read this file

Each contract lists: where it lives, what is frozen, and what is explicitly *not* frozen
(so contributors don't over-constrain themselves). Tests marked 🔒 pin the contract in CI.

---

## 1. Safety policy — `ironcore/safety/`

**Frozen:**
- `Mode` values: `plan`, `manual`, `accept-edits`, `auto`; `CYCLE` order
  `manual → accept-edits → auto → plan`; boot default MANUAL.
- `ToolRisk` values: `read`, `write`, `exec`, `net`. One risk per tool, worst-case honest.
- `Decision` values: `allow`, `ask`, `deny`; the full `POLICY` table as written.
- Invariants: reads always allowed; NET never `allow`; PLAN denies all mutation; layered
  policies may only tighten. 🔒 `tests/test_safety.py`
- `decide(mode, risk)` is the **only** gate; the engine may not construct decisions.

**Not frozen:** `DENYLIST_SEED` contents (IC-402 grows it); command-classifier internals.

## 2. Provider — `ironcore/providers/base.py`

**Frozen:**
- `Message`, `ToolCall`, `StreamEvent`, `CompletionResult`, `SamplingPolicy` field names and
  types (additive fields allowed with defaults).
- `Provider.complete/stream/list_models` signatures; `stream` must terminate with a `done`
  or `error` event; providers raise `ProviderError` only (never transport exceptions).
- Malformed model output is repairable data (an `error` event with `repairable: true`),
  not an exception.
- `MockProvider` remains a drop-in for every consumer — nothing may require a concrete
  provider class. 🔒 `tests/test_providers.py`

**Not frozen:** retry/backoff internals; extra kwargs on concrete constructors.

## 3. Tool — `ironcore/tools/base.py`

**Frozen:**
- `Tool` attributes (`name`, `description`, `risk`, `parameters`) and `async run(**kwargs)
  -> ToolResult`.
- `ToolResult(ok, output, error, data)` semantics: `output` is model-visible text (already
  truncated/redacted by the engine); `data` is harness-only.
- Tools never print, never prompt, never self-gate; READ tools are side-effect-free.
- `ToolRegistry.specs()` emits OpenAI function-call format. 🔒 `tests/test_tools.py`

**Not frozen:** the concrete tool lineup and their parameter schemas (owned by phase-3 tasks).

## 4. Engine & events — `ironcore/core/`

**Frozen:**
- Event dataclass names and required fields in `core/events.py` (additive only). The event
  stream is the ONLY core→front-end channel; approval answers flow back via the engine's
  approval future, nothing else.
- Engine invariants: every tool call gated; every provider call composed; engine never
  prints/prompts; `TurnCompleted.stop_reason` computed from tool evidence, not model claims.
- `TurnEngine.__init__(provider, tools, settings, profile, mode)` and
  `run_turn(user_input) -> AsyncIterator[Event]`.

**Not frozen:** internal state-machine implementation; context-composer heuristics (IC-501
owns them, then freezes the *budget shares* here).

## 5. Envelope — `ironcore/envelope/profile.py`

**Frozen:**
- `CapabilityProfile` field names (additive allowed); JSON persistence via
  `save/load(envelope_dir, model_id)`; slug scheme.
- Ladder orders and thresholds: tool `native 0.95 / strict_json 0.90 / text floor`; edit
  `unified_diff 0.90 / search_replace 0.85 / whole_file floor`; `anchor_cadence` clamp
  [2, 12]. The engine selects protocols exclusively via `recommended_*`.
  🔒 `tests/test_envelope.py`
- Unprobed models get floor-conservative defaults.

**Not frozen:** probe implementations and trial counts (must only *fill* these fields).

## 6. Slash commands — `ironcore/commands/base.py`

**Frozen:**
- `SlashCommand(name, summary, usage, handler, implemented)`; handlers are synchronous
  `(CommandContext, args) -> str` and must not block (long work is scheduled, not awaited).
- `CommandRegistry.dispatch("/name args", ctx)` semantics; `UnknownCommand` on miss.
- `CommandContext` mutation is the only side channel (`settings`, `mode`, `goal`, `extra`).
  🔒 `tests/test_commands.py`

**Not frozen:** the command lineup; handler bodies.

## 7. Config — `ironcore/config/settings.py`

**Frozen:**
- Precedence: defaults ← user toml ← project toml ← env. Env names `IRONCORE_BASE_URL`,
  `IRONCORE_MODEL`, `IRONCORE_API_KEY`, `IRONCORE_MODE`.
- Section/key names shown in SPEC §12 (additive allowed).
- Project config may never *raise* autonomy above the user-config ceiling (SAFETY.md T8).
  🔒 `tests/test_config.py` (ceiling test lands with IC-402)

## 8. Handoff format — `ironcore/memory/handoff.py`

**Frozen:**
- Sentinels `<!-- HANDOFF v1 BEGIN/END -->`; header line `## Handoff — <ts> — <author>`;
  fields Context/Changed/Verified/Next/Gotchas; append-only files.
  🔒 `tests/test_handoff.py`
- Format changes = `v2` sentinels + a reader that still parses v1.

## 9. Workflow schema — `ironcore/workflows/`

**Frozen after IC-902 lands** (schema is draft until then; the sketch in `engine.py` is the
starting point). Already binding: workflows are YAML in `.ironcore/workflows/`; the model
never controls orchestration flow; subagent outputs validate against declared schemas.

## 10. IRONCALL text protocol

**Frozen after IC-606 lands.** Already binding: fenced ```` ```ironcall ```` blocks with a
JSON body `{"tool": ..., "args": {...}}`; one call per block; results return in
```` ```ironresult ```` blocks; parser is fence-regex + `json.loads` + bounded repair.
