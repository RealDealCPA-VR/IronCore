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
- `complete`/`stream` accept optional keyword-only `response_format` + `extra_body`
  (default `None`) — additive, backward-compatible guided-decoding knobs merged into the
  request body (`extra_body` wins a key clash); `MockProvider` records them.
- *Additive (MS-3):* `MockProvider` also records the per-call `sampling` policy —
  `last_sampling` (most recent) and `sampling_calls` (every call, in order) — the same way
  it records `last_response_format`, so tests can prove which profile sized each call's
  `max_tokens`/temperature (per-role windows; best-of-N resampling consumes the list).
  *Migration:* none — recording only; no request/response behavior changes.
- *Additive (MS-6):* `Message.images: list[ImageData]` (default empty) and
  `ImageData(base64, media_type)` are frozen wire types. Non-empty `images` serialize as
  OpenAI content-parts — one `image_url` part per image carrying a base64 `data:` URI,
  plus a `text` part when `content` is non-empty; messages without images keep the exact
  plain-string content shape. Providers never raise on images (a server that rejects them
  surfaces the normal `ProviderError` path), and `MockProvider` records image-bearing
  messages via `calls` like any other. *Migration:* none — purely additive with a
  default; every existing `Message` construction and `dataclasses.replace` call is
  unchanged.
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
- *Additive (MS-2):* `TurnEngine.repoint(provider, profile)` hot-swaps the live provider +
  capability profile between provider calls (`/model` live switches); `__init__`/`run_turn`
  signatures are unchanged and protocol selection still flows only through
  `profile.recommended_*`. *Migration:* none — existing constructors and turn flows are
  untouched; old provider instances stay open (the `ProviderRegistry` owns their lifecycle
  via `close_all`).
- *Additive (MS-3):* `TurnEngine.__init__` additionally accepts keyword-only
  `roles: RoleRouter | None = None` (`core/roles.py`, default `None`). When set, each
  provider call resolves its (provider, profile) by role — `planner` for PLAN-mode turns,
  `coder` otherwise, `summarizer` for compaction; an UNSET role always uses the engine's
  primary `(provider, profile)` pair, so zero-config behavior is unchanged. Protocol
  selection still goes exclusively via the ACTIVE profile's `recommended_*` (§5).
  *Migration:* none — existing constructors pass no `roles` and stay byte-identical;
  routing degrades to the primary pair (never an error) when a role cannot resolve.
- *Additive (MS-4):* `ResampleProgress(turn_id, seam, attempt, total)` is an additive event
  emitted while racing best-of-N candidates at a mechanically-verified seam (`seam` is
  `"parse"` or `"edit"`); front ends may ignore it. Raced winners still pass the §1 gate —
  `decide()` remains the only path to a tool. *Migration:* none — the event vocabulary is
  additive-only, and the default config (`[engine] best_of_n = 1`, §7) never emits it.

**Not frozen:** internal state-machine implementation; context-composer heuristics (IC-501
owns them, then freezes the *budget shares* here).

## 5. Envelope — `ironcore/envelope/profile.py`

**Frozen:**
- `CapabilityProfile` field names (additive allowed); JSON persistence via
  `save/load(envelope_dir, model_id)`; slug scheme. `source` (`"default" | "seeded" |
  "probed" | "tuned"`) is an additive provenance field, default `"default"`.
  `chars_per_token` (`float`, default `4.0`) is an additive measured field: the composer's
  token estimator divides character counts by it; unprobed and seeded profiles keep the
  `4.0` default. *Migration (MS-1):* envelope JSONs cached before this field load as `4.0`
  via the pydantic default — byte-identical legacy packing; no re-probe required.
  `vision` (`bool`, default `False`) is an additive capability field: floor default
  `False`, seeded from endpoint introspection (Ollama `/api/show` `capabilities`),
  preserved through `run_probes(base=...)`; `[envelope] vision` in config overrides it.
  The engine/tools consult it ONLY for image attachment — never for protocol selection.
  *Migration (MS-6):* envelope JSONs cached before this field load as `False` via the
  pydantic default; no re-probe required.
- Ladder orders and thresholds: tool `native 0.95 / strict_json 0.90 / text floor`; edit
  `unified_diff 0.90 / search_replace 0.85 / whole_file floor`; `anchor_cadence` clamp
  [2, 12]. The engine selects protocols exclusively via `recommended_*`.
  🔒 `tests/test_envelope.py`
- Unprobed models get floor-conservative defaults.
- *Additive (MS-8):* `source` value `"tuned"` — a measured profile whose ladder scores were
  conservatively LOWERED from live-session evidence (`envelope/outcomes.py`). The tuner may
  only lower scores / `coherence_horizon`, never raise them; `probed_at` is preserved; and
  the ladder orders/thresholds above stay the sole selection mechanism (tuning only edits
  the *scores* the frozen `recommended_*` functions read). Evidence persists in a sibling
  `<slug>.outcomes.json` sidecar (`OutcomeLedger`) next to the envelope JSON; a missing or
  corrupt sidecar loads as a fresh ledger — reads never raise. The tuned overlay is
  recomputed at load time and never written back to the envelope JSON (the cached profile
  stays the honest measurement). *Migration:* none — existing envelope JSONs and ledgers
  need no changes; profiles never persist `source="tuned"`, and consumers matching on
  `source` should treat `"tuned"` as measured-and-adjusted, not unprobed.

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
- *Additive (MS-4):* `[engine] best_of_n` (int, default 1 = disabled, validated 1..5) is an
  additive section/key: up to N-1 extra candidates are raced per turn when a mechanical
  verifier fails (a tool call the repair ladder gives up on; an `edit_file` patch that does
  not apply), each charged to the turn budget. Resampled candidates still pass the §1 gate.
  *Migration:* none — configs without `[engine]` behave byte-identically.
- *Additive (MS-7):* `[mcp]` — `[mcp.servers.<name>]` tables (`command`, `args`, `env`,
  `url`, `timeout_s`, `enabled`; `command` or `url` required) configure MCP tool servers;
  the reference block lives in SPEC §12. v1 connects stdio (`command`) servers only —
  url-only entries parse but are skipped with a note. Their tools register as
  `mcp__<server>__<tool>` at `ToolRisk.NET` (no new risk value): like every NET tool they
  are never registered unless `safety.network_tools` is true, and §1 gating applies
  unchanged (NET never auto-allowed, denied in PLAN). *Migration:* none — configs without
  `[mcp]` behave byte-identically, and no existing key changes meaning.
- Project config may never *raise* autonomy above the user-config ceiling (SAFETY.md T8).
  🔒 `tests/test_config.py` (ceiling test lands with IC-402)

## 8. Handoff format — `ironcore/memory/handoff.py`

**Frozen:**
- Sentinels `<!-- HANDOFF v1 BEGIN/END -->`; header line `## Handoff — <ts> — <author>`;
  fields Context/Changed/Verified/Next/Gotchas; append-only files.
  🔒 `tests/test_handoff.py`
- Format changes = `v2` sentinels + a reader that still parses v1.

## 9. Workflow schema — `ironcore/workflows/schema.py`

**Frozen (IC-902).** Pydantic v2 models + loader; the model never controls orchestration flow.

- **`Workflow`**: `name: str` (required), `description: str = ""`, `inputs: list[str] = []`,
  `phases: list[Phase]` (required, **non-empty**, **unique `id`s**). Extra keys forbidden.
- **`Phase`**: `id: str`, plus **exactly one** phase-kind — `fanout: Fanout`,
  `foreach: str`, or `reduce: str | dict`. A `foreach` phase carries its subagent in the
  sibling `agent: AgentSpec` field and its value must be a `{{...}}` reference; a top-level
  `agent` is invalid on any non-foreach phase. Extra keys forbidden.
- **`Fanout`**: `items: list`, `agent: AgentSpec` (agent nests inside `fanout:`).
- **`AgentSpec`**: `role: str`, `prompt: str` (a `{{var}}` template), `output_schema: dict | None
  = None` (an inline schema mapping, **not** a string ref). Extra keys forbidden.
- **`{{...}}` rule**: prompts/refs use `{{var}}` and dotted `{{phase.field}}` placeholders;
  `interpolate(template, context) -> str` walks nested dicts and raises `WorkflowError` naming
  any unresolved variable. Substitution is a pure harness op — never model-driven.
- **Loading**: `load_workflow(path_or_text: str | Path, *, source=None) -> Workflow`
  (str = YAML text, Path delegates to file), `load_workflow_file(path) -> Workflow`,
  `discover_workflows(dir) -> dict[str, Path]` (stem→path; `.yaml`/`.yml`; `.yaml` wins a clash;
  missing dir → `{}`). YAML is parsed with **`yaml.safe_load` only — never `yaml.load`** (files
  are untrusted; tags must not construct objects). All syntax **and** schema failures surface as
  a `WorkflowError` with a message naming the file/field — callers never see a raw
  `yaml.YAMLError`/pydantic `ValidationError`. 🔒 `tests/test_workflow_schema.py`

**Not frozen:** the reducer registry and `output_schema` validator internals (IC-903); the
built-in workflow set (IC-905); `WorkflowRunner` execution semantics.

## 10. IRONCALL text protocol

**Frozen after IC-606 lands.** Already binding: fenced ```` ```ironcall ```` blocks with a
JSON body `{"tool": ..., "args": {...}}`; one call per block; results return in
```` ```ironresult ```` blocks; parser is fence-regex + `json.loads` + bounded repair.

## 11. Plugin entry points — `ironcore/plugins.py`

**Frozen (MS-5):**
- The five entry-point group names: `ironcore.providers`, `ironcore.tools`,
  `ironcore.commands`, `ironcore.probes`, `ironcore.edit_formats`.
- What each group's entry point must resolve to:
  - `ironcore.tools` — `factory(settings, workspace) -> Tool | Sequence[Tool]`; every
    produced tool must be a `Tool` (§3) with a nonempty `name`, a real `ToolRisk` member
    as `risk`, and a dict `parameters` whose `spec()` does not raise.
  - `ironcore.commands` — a `SlashCommand` or sequence of them (the repo's `COMMANDS`
    tuple convention; the entry point IS the object, no factory call).
  - `ironcore.probes` — a ZERO-ARG factory returning `Probe | Sequence[Probe]`
    (duck-typed: str `id`/`title`, `targets` list/tuple of dotted profile paths, async
    `run(provider)`). Plugin probes only *fill* profile fields via the runner's
    dotted-path merge; protocol/format selection stays `recommended_*` (§5), and the
    seven built-in probe ids are reserved.
  - `ironcore.providers` — `factory(base_url=, api_key=, model=[, transport=]) ->
    Provider`, selected when `provider.type` equals the entry-point name
    (`auto`/`ollama`/`openai` are reserved). It is constructed through the registry's
    single `_build` path, so `for_role` (MS-3) and `for_model` (MS-2) construct plugin
    providers too; an unmatched `provider.type` keeps the pinned unknown-type → auto
    fallthrough (`doctor` warns, boot never breaks).
  - `ironcore.edit_formats` — `apply(original_text, edit) -> PatchResult`, registered
    under the entry-point name (`^[a-z][a-z0-9_-]{0,31}$`; the built-in ladder rungs are
    reserved and always win a clash).
- Fail-safe rules: a broken plugin is skipped and recorded (`ironcore doctor` lists each
  skip with its reason), never a boot crash; discovery order is deterministic (sorted by
  entry-point name). Built-ins win every duplicate-name clash (tools — including
  `read_image` — commands, and edit formats). The safety kernel is NOT extensible:
  plugin tools pass the same `decide(mode, risk)` gate (§1), and NET-risk plugin tools
  are not even loaded unless `safety.network_tools` (the `fetch_url` rule).
- Plugin edit formats: a mechanical apply failure flows through `edit_file`'s normal
  failure branch and carries the same `data={"patch_failure": True, "format": …}`
  payload built-in formats emit — but in v0.x plugin formats are **never
  auto-recommended** (the §5 ladders are closed), **never pre-verified by best-of-N
  resampling** (§4 — a resample winner must be in a built-in format), and **never
  tuned** (§5 — the tuner reads ladder rungs only).
- `[plugins] enabled = false` (an additive §7 section, default `true`) disables
  discovery entirely — `entry_points` is never consulted. 🔒 `tests/test_plugins.py`

**Not frozen:** validation internals and skip-reason wording; `LoadedPlugins`' exact
shape; doctor output formatting.

*Migration:* none — with no plugin distributions installed every surface behaves
byte-identically: `plugins=` / `extra_probes=` / `provider_factory=` / `extra_formats=`
are additive parameters defaulting to None/absent, and configs without `[plugins]`
load as `enabled = true` via the pydantic default.
