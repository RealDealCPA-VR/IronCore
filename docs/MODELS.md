# Models: the Capability Envelope in depth

> How IronCore turns "some model at some endpoint" into "a measured set of things we can
> rely on." Companion to SPEC.md §4; implementation in `ironcore/envelope/`.

## 1. Why measure

Open-model capability claims are unreliable in exactly the dimensions an agent harness cares
about. Advertised 128k contexts with retrieval collapse past 16k; "supports function calling"
that emits malformed JSON one call in five; chat templates that silently eat system prompts.
Two quantizations of the *same* model diverge on all of these. So IronCore treats capability
as an empirical property of `(model, endpoint, quantization)` — probed once, cached, refreshed
on `/probe` or a model digest change.

## 2. The probe suite

Declared in `envelope/probes.py`, run by `envelope/runner.py` (IC-602..604, shipped). Design
rules: mechanical scoring only (no LLM judges), fixed seeds where supported, ≤ ~2 min total on
a local 30B, partial failure degrades scores rather than blocking.

| Probe | Method | Fills |
|---|---|---|
| `CTX-HONESTY` | plant key-value needles at depths 25/50/75/90% of window sizes stepping up (4k→8k→16k→…); ask for exact retrieval; honest_context = largest size with ≥0.9 retrieval | `honest_context` |
| `RETENTION` | turn 1 sets an arbitrary constraint ("prefix every answer with REF-7"); filler turns; check adherence at turns 3/6/9/12 | `instruction_retention`, `coherence_horizon` |
| `TOOL-FORM` | 10 trials per protocol (native, strict_json, ironcall) requesting a specific call with specific args; score = parseable AND correct name AND correct args | `tool_protocols` |
| `JSON-STRICT` | 10 trials: emit JSON matching a schema with distracting instructions in the payload text; mechanical validation | `json_adherence` |
| `EDIT-FORMAT` | per format: given a fixture file + change request, emit the edit; score = the harness patcher applies it AND result compiles | `edit_formats` |
| `CODE-SMOKE` | write a ~10-line function from a docstring + make a failing test pass; floor gate | usability flag |
| `TOKEN-RATIO` | send filler docs of known char counts; read the server-reported `prompt_tokens`; ratio = chars/tokens, clamped [1.0, 8.0]; a server that omits usage keeps the 4.0 default | `chars_per_token` |

### 2.1 Seeded, not probed: the instant-on profile

The deep suite takes minutes; two signals are free at boot. `envelope/seed.py` assembles a
**provisional but usable** profile in ~1–2s and the deep probe then *refines* rather than
replaces it (`run_probes(base=...)`). Off switch: `[envelope] instant_seed = false`.

| Field | Seeded from | Notes |
|---|---|---|
| `context_window`, `honest_context` | Ollama `/api/show` | `honest_context` = the server's pinned `num_ctx` when set (the real ceiling), else the advertised window capped at 32768 — never seed a depth nobody measured. |
| `tool_protocols`, `edit_formats` | endpoint capability detection | Native tool-calling detected → `native 0.95` + `search_replace 0.85` (a usable middle rung); otherwise the text / whole-file floors. |
| `vision` | Ollama `/api/show` `capabilities` array | **There is no VISION probe.** A server that omits the array honestly keeps the floor default (`false`). Override with `[envelope] vision` for endpoints without introspection (e.g. vLLM serving a VL model). |

The seed is deliberately optimistic where the endpoint gives a signal — the deep probe
corrects it within minutes, and the repair loop plus downgrade ladders absorb an over-optimistic
seed. It is **never cached**: only measured profiles are written to disk, so `probed_at` stays
`None` and the deep probe still runs.

### 2.2 Provenance: the `source` field

Every profile says where its numbers came from, and `/envelope` prints it:

| `source` | Meaning |
|---|---|
| `default` | Floor-conservative. Nothing measured, nothing introspected. |
| `seeded` | Introspected at boot (§2.1). Provisional — a measurement is in flight. |
| `probed` | Measured by the suite. |
| `tuned` | Measured, then conservatively *lowered* by live evidence (§8). Treat as measured-and-adjusted, never as unprobed. |

## 3. The ladders (implemented, frozen)

From `envelope/profile.py` — the engine may not choose protocols any other way:

```
tool calls: native ≥ 0.95 → strict_json ≥ 0.90 → text_protocol (floor, always works)
edits:      unified_diff ≥ 0.90 → search_replace ≥ 0.85 → whole_file (floor)
anchors:    every clamp(coherence_horizon, 2, 12) turns
context:    budget against honest_context, never the advertised window
```

Thresholds are deliberately strict: a 90%-reliable protocol still fails one call in ten, and
agent sessions make hundreds of calls. Falling one rung costs tokens; staying on a flaky rung
costs correctness.

Unprobed model → floor everything (text protocol, whole_file, 4k honest context). IronCore is
safe-slow before it is measured-fast.

## 4. Compensation patterns by weight class

Guidance, not gospel — the envelope decides per model. What typically happens:

**~7B (qwen3:8b, llama3.2, phi-4 class).** Text protocol; search/replace or whole-file edits;
anchor every 2–3 turns; heavy micro-stepping (one file, one change per step); verification
after every mutation; excellent as summarizer/verifier roles in a routed setup.

**~14–32B (qwen3-coder:30b, devstral, gemma3-27b class).** The IronCore sweet spot. Often
strict_json or even native tools; search/replace edits reliably; anchor every 4–6 turns;
micro-stepping for cross-file work; best-of-2 with mechanical verification on hard steps.

**~70B+ (llama3.3-70b, qwen3-235b-a22b class, hosted OSS).** Native tools common; unified
diffs mostly land; anchor every 8–12 turns; the harness's job shifts from compensation to
insurance — verification and budgets still catch the tail failures.

**Reasoning variants (deepseek-r1 distills, qwen3 thinking modes).** Long thinking hurts
interactivity; envelope records latency; router (§5) points them at planner/verifier roles,
with think-suppression flags where the server supports them.

## 5. Role routing

`[roles]` config (SPEC §12): planner / coder / summarizer / verifier may each be a different
model. The composer already produces role-scoped, self-contained contexts, so routing is free.
Patterns that work: 70B plans + 30B executes (quality plans, fast iterations); 30B codes + 8B
summarizes/compacts (latency); anything + *different-family* verifier (decorrelated errors —
a model grading its own homework shares its own blind spots).

**Each role, measured (MS-3).** The engine's `RoleRouter` (`core/roles.py`) resolves every
routed role to its own provider *and its own capability envelope*, loaded from the same
per-model cache (`~/.ironcore/envelopes/<slug>.json`) that `/probe` and `/model` write —
so a routed coder runs on **its** measured wire protocol, honest context, and sampling, and
the composer/compaction budgets against **its** window (a small-window coder compacts
earlier: that is the window being composed into). An unmeasured role model honestly runs on
floor-conservative defaults; measure it into the shared cache by switching to it once with
`/model <role-model>` and back. `/envelope` appends a per-role status tail. Worked example:

```toml
[provider]
model = "qwen3-coder:30b"        # the primary — plans in PLAN mode unless routed

[roles]
planner = "llama3.3:70b"          # PLAN-mode turns think on the 70B
coder   = "qwen2.5-coder:7b"      # every other turn executes on the fast 7B
# summarizer / verifier unset -> the primary model handles them
```

## 6. Sampling

Envelope stores working defaults per model. Harness policy: temperature 0.1–0.3 for tool
turns and edits; up to 0.7 only for brainstorm-type asks; retries resample at +0.2 (escape
deterministic failure modes); best-of-n reserved for steps with a mechanical verifier
(n answers → run the check → first pass wins). Best-of-N shipped as `[engine] best_of_n`
(default `1` = off, max 5): it fires only at the two mechanically verified seams — a tool call
the repair ladder gave up on, and an edit that would not apply — and every candidate is
charged to the turn budget and still passes the safety gate.

## 7. Endpoint notes

- **Ollama**: `/v1` for chat; native `/api/show` for true context length + quant (feeds the
  envelope); watch `num_ctx` — server-side default may be far below the model's window.
  Keep-alive managed so interactive sessions don't reload weights (IC-203).
- **llama.cpp server**: GBNF grammars = the strongest strict_json rung available anywhere.
- **vLLM**: guided decoding (`guided_json`) similarly strong; high-throughput best-of-n.
- **Hosted OSS (OpenRouter/Together/Groq)**: treat as capable but *rate-limited*; envelope
  records latency; NET-adjacent secret hygiene matters more (SAFETY.md §6). These endpoints
  **require a real `[provider] api_key`** — set `IRONCORE_API_KEY` in your shell rather than
  writing it into a file ([CONFIG.md](CONFIG.md) §2).

## 8. The self-improvement loop (MS-8)

The probe measures once. Live sessions produce the same *mechanical* evidence forever, so
`envelope/outcomes.py` records it and folds it back — carefully.

**What is recorded.** Per model, per profile generation: every provider-call iteration is one
tool-protocol sample at the *active* rung; every real edit apply (success or a mechanical
`patch_failure`) is one edit-format sample; verification pass/fail and turn drift are recorded
alongside. Nothing about the *content* of your work is stored — only counters.

**Where it lives.** `~/.ironcore/envelopes/<slug>.outcomes.json`, a sidecar next to that
model's profile. Writes are atomic; a missing or corrupt sidecar loads as a fresh ledger, and
reads never raise. Delete it to forget everything IronCore learned about a model.

**How it is applied — downgrade only.** At session start `apply_tuning` may *lower* a ladder
score or `coherence_horizon` that live evidence contradicts. It may never raise one: an
upgrade needs a real measurement, so a suspiciously clean live rate emits a "run `/probe`"
hint and nothing else. The frozen §3 ladders stay the sole selector — tuning edits the scores
they read, so a lowered score makes the ladder fall a rung by itself. Hysteresis (minimum
sample floors) stops one bad turn from moving anything, counters halve past a cap so old
evidence decays, and the whole ledger resets whenever the profile generation changes — stale
evidence must never re-downgrade a freshly measured profile.

**What is not touched.** The tuned profile is an overlay recomputed at load time and never
written back to the envelope JSON: the cached measurement stays honest, and `/probe` always
re-measures from scratch. `/envelope` reports `source = tuned` rather than hiding the
adjustment.

**Off switch.** `[envelope] auto_tune = false` — no recording, no tuning.
