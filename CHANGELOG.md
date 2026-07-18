# Changelog

All notable changes to IronCore are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-07-17

The moonshots release: every bet in the README's Moonshots section has landed —
five that mold the harness *deeper* to the measured model, two that take it
beyond text, and one that opens it up — plus the two envelope upgrades shipped
since 0.1.0 (instant-on profiling and real server-side guided decoding).
**1546 tests, offline-first (no model, no network).**

### Molds deeper
- **Model-aware tokenization.** The probe battery now *measures* each model's
  chars-per-token ratio (a `TOKEN-RATIO` probe: known-char filler docs vs the
  server's reported `prompt_tokens`), and the context composer + compaction
  predicate budget with it — the universal `chars/4` guess remains only as the
  honest default for servers that don't report usage. The `/envelope` report
  card gained a `Token ratio:` line.
- **Live model swaps.** `/model <name>` re-points the *running* session
  mid-conversation: a measured model hot-swaps its profile instantly from the
  on-disk envelope cache; an unmeasured one runs on floor defaults while it is
  seeded and deep-probed in the background. The cache remembers every model
  you've measured, and `/model` marks them.
- **A model per role, each measured.** The `[roles]` config now routes the turn
  loop itself — Plan-mode turns run on the planner model, execution turns on
  the coder, compaction on the summarizer — each with *its own* capability
  envelope from the shared cache, so every routed call uses that model's
  measured wire protocol, context window, and sampling (floor defaults,
  honestly, until a role model is measured). `/envelope` shows per-role status.
- **Best-of-N escape hatches.** When the model dead-ends at a seam with a
  *mechanical* verifier — a tool call that won't parse, a patch that won't
  apply — the engine resamples up to `[engine] best_of_n` candidates at raised
  temperature and races them: the first that parses / applies in-memory
  re-enters the normal safety gate; losers are discarded, and every candidate
  is charged to the turn budget. Off by default (`best_of_n = 1`).
- **The self-improvement loop.** Every session records mechanical evidence per
  model — did tool calls parse at the active rung, did edits apply, did
  verification pass, did the turn drift — into an outcome ledger next to the
  envelope cache, and at session start a deterministic tuner conservatively
  *lowers* any ladder score the live evidence contradicts (downgrade-only:
  upgrades are never applied, they earn a "run `/probe`" hint). The report card
  and `/envelope` say `tuned` honestly; off switch: `[envelope] auto_tune`.
- **Guided decoding — the real `strict_json` rung.** A model the envelope routes
  to `strict_json` is now driven with server-side constrained decoding: the
  engine sends `response_format` (a json-schema pinning output to one
  `{"tool","args"}` call) so the model emits *guaranteed* well-formed tool calls
  instead of best-effort ones — with a `done` action so a constrained model can
  still finish a turn, the raw JSON scaffold suppressed from the transcript, and
  a clean ladder-down to the IRONCALL text floor if the server can't constrain.
  The `Provider` gained additive `response_format`/`extra_body` knobs (vLLM
  `guided_json`/llama.cpp GBNF via `extra_body`), and the capability probe now
  measures *guided* reliability so the ladder routes here only when it works.
- **Instant-on profiling.** On first use of an unprobed model, IronCore now
  seeds a *usable* capability profile in ~1 second from endpoint introspection
  (Ollama `/api/show` for the real context window + capability detection) — the
  first turn runs with the model's true window and native tool-calling instead
  of the conservative floor — then deepens the measurement with the full probe
  battery in the background and hot-swaps the refined profile in. A `source`
  field (`default`/`seeded`/`probed`) makes the report card and `doctor` honest
  about whether values are introspected guesses or measurements. Configurable
  via `[envelope] instant_seed` / `auto_probe`.

### Beyond text
- **Vision — image inputs for screenshots/diagrams.** A new `read_image` tool
  lets the model actually look at a workspace PNG/JPEG/GIF/WEBP: the bytes ride
  the conversation as OpenAI image content-parts (base64 data URIs, so Ollama
  and vLLM vision models both work). The capability is *measured, not assumed*
  — seeded from Ollama's `/api/show` capabilities (`[envelope] vision`
  overrides for endpoints without introspection), and a text-only model gets an
  honest "no vision capability" error instead of a hallucination. The composer
  budgets attached images and keeps only the newest two; the report card gained
  a `Vision: yes|no` line.
- **MCP tool servers.** `[mcp.servers.<name>]` config entries connect stdio
  MCP servers through a dependency-free JSON-RPC client (spawned directly,
  never via a shell); their tools register as `mcp__<server>__<tool>` at NET
  risk — never auto-approved, denied in Plan mode, and only present at all
  when `safety.network_tools = true`. Output is fenced UNTRUSTED like every
  tool, and `ironcore doctor` reports the configured server lineup.

### Extensibility
- **Drop-in plugins.** Providers, tools, slash commands, probes, and edit
  formats now plug in as standard Python entry points (`ironcore.providers` /
  `ironcore.tools` / `ironcore.commands` / `ironcore.probes` /
  `ironcore.edit_formats`): `pip install` a plugin distribution next to
  IronCore and its tools register behind the *same* safety gate, its provider
  builds when `provider.type` names it (role routing and `/model` swaps
  included), its probes join `/probe`'s battery, and its edit formats join
  `edit_file`. Built-ins win every name clash, a broken plugin is skipped and
  reported by `ironcore doctor` — never a crashed boot — and
  `[plugins] enabled = false` turns discovery off. Author guide:
  [`docs/PLUGINS.md`](docs/PLUGINS.md).

## [0.1.0] — 2026-07-16

The first release: a complete, interactive terminal coding agent built for
open-source models. Every phase of the build plan ([TODO.md](TODO.md)) is
shipped, validated by an independent adversarial review with execution-verified
probes, and proof-tested end-to-end against real files, subprocesses, and git.
**1223 tests, offline-first (no model, no network).**

### The Capability Envelope
- Measured model profiling — `CTX-HONESTY`, `RETENTION`, `TOOL-FORM`,
  `JSON-STRICT`, `EDIT-FORMAT`, `CODE-SMOKE` probes fill a `CapabilityProfile`
  that picks the tool protocol, edit format, context budget, and anchor cadence
  per model, all with mechanical scoring.
- Adapter ladders: native → strict-JSON → **IRONCALL** text protocol for tool
  calls; unified-diff → search/replace → whole-file for edits; each degrades to
  a format the model can actually produce, never failing the task.

### Turn engine
- The deterministic `COMPOSE → CALL → PARSE → GATE → EXECUTE → OBSERVE → VERIFY
  → DONE` loop: the harness owns all state and re-presents it, so the model
  never has to remember.
- Malformed-output repair with protocol laddering; a verification loop that
  feeds failures back once and **refuses to report unverified work as done**
  (`goal-unmet`); budget/runaway protection; micro-stepping and history
  compaction.

### Safety kernel
- Four modes (Plan / Manual / Accept-Edits / Auto) cycled with Shift+Tab; a
  Mode×Risk policy where network is never auto-allowed and Plan denies all
  mutation.
- Path jail, command policy (deny-lists in every mode + risky-pattern
  escalation), an approval broker with turn-scoped grants, secret redaction,
  a prompt-injection guard, and byte-exact git-snapshot undo.

### Tools & providers
- read / list / glob / grep, a fuzzy deterministic patcher with jailed atomic
  writes, a cross-platform shell with process-tree kill, and a gated network
  fetch.
- One streaming OpenAI-compatible client (Ollama, vLLM, llama.cpp, LM Studio,
  OpenRouter, Together, Groq) with retries and key redaction; Ollama
  introspection; role routing; endpoint capability detection.

### Interactive TUI
- A streaming Textual app: transcript with live tool cards, an approval modal
  with a colored diff viewer, Shift+Tab mode switching, a slash-command palette,
  and resumable sessions.
- Slash commands: `/goal`, `/loop`, `/model`, `/init`, `/compact`, `/undo`,
  `/redo`, `/review`, `/memory`, `/workflow`, `/help`, `/mode`, `/version`.

### Workflows & memory
- Deterministic multi-agent workflows (YAML): fanout / foreach / reduce over
  fresh-context subagents, harness-controlled flow, per-item failure isolation,
  and three built-ins (`review`, `migrate`, `explain-repo`). Subagents inherit
  the session mode — a Plan session can never mutate through a workflow.
- `IRONCORE.md` project memory injected into the system context; handoff blocks
  written on compaction and session end; JSONL session transcripts with resume.

### Distribution
- Pure-Python wheel (`pip` / `uv tool` / `pipx`), CI on Ubuntu + Windows across
  Python 3.11 and 3.13 with an 85% coverage gate on the core packages, and a
  tag-triggered PyPI release via trusted publishing.
- `ironcore demo` — a fully offline narrated walkthrough of a real session.

<!--
Release links are only added once the tag actually exists. 0.1.0 was never tagged and is
not being tagged retroactively, so it has no link on purpose — a link that 404s is worse
than no link. Pushing a `v*` tag runs .github/workflows/release.yml, which creates the
GitHub Release (sdist + wheel attached) using the matching section above as its notes.
-->

[0.2.0]: https://github.com/RealDealCPA-VR/IronCore/releases/tag/v0.2.0
