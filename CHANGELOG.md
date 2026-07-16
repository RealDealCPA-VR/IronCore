# Changelog

All notable changes to IronCore are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Instant-on profiling.** On first use of an unprobed model, IronCore now
  seeds a *usable* capability profile in ~1 second from endpoint introspection
  (Ollama `/api/show` for the real context window + capability detection) — the
  first turn runs with the model's true window and native tool-calling instead
  of the conservative floor — then deepens the measurement with the full probe
  battery in the background and hot-swaps the refined profile in. A `source`
  field (`default`/`seeded`/`probed`) makes the report card and `doctor` honest
  about whether values are introspected guesses or measurements. Configurable
  via `[envelope] instant_seed` / `auto_probe`.

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
- `python -m demo` — a fully offline narrated walkthrough of a real session.

[0.1.0]: https://github.com/RealDealCPA-VR/IronCore/releases/tag/v0.1.0
