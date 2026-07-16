# Implementation Plan — Instant-on Profiling

> **STATUS: SHIPPED (2026-07-16).** Built via a doer→validator swarm; both waves
> validated SHIP by execution. `envelope/seed.py`, the app seed→probe flow, the
> `source` provenance field, config `instant_seed`, and honest report-card/doctor
> display are live and pinned by `tests/test_envelope_seed.py` +
> `tests/test_e2e_instant_on.py`. Plan retained as the design record.

> Eliminate the cold-probe wait: on first use of a model,
> produce a **usable** CapabilityProfile in ~1 second from endpoint introspection
> (Ollama `/api/show` + capability detection), then refine it with the full probe
> suite in the background — hot-swapping as measurements land. No wait at all.

## Problem

Today (post-review): an unprobed model boots on the **floor** default profile
(text protocol, whole-file edits, 4096 honest context) and a full ~2-minute
probe runs in the background. So for the first two minutes, even a capable model
that clearly does native tool-calling is driven with the slowest, most
conservative settings. The introspection signals that could bootstrap a good
profile in ~1s — Ollama's real context window (`/api/show`) and endpoint
capability detection (`detect()`) — exist but are unused at boot.

## Target flow (first use of an unprobed model)

```
t=0     floor default profile (instant, 0 model calls)      ← usable in <1s
t≈1s    SEED: /api/show (context) + detect() (capabilities) ← hot-swap #1
        → first real turns use the REAL window + native tools (provisional)
t≈1-2m  DEEP PROBE: full 6-probe suite, base = the seed     ← hot-swap #2
        → measured profile, cached to ~/.ironcore/envelopes → future boots load it
```

Both steps run in the background; the user can type immediately. The seed is
**provisional** (never cached); only the measured profile is saved, so a later
boot skips straight to it.

## The seed: optimistic-but-correctable

The seed is deliberately optimistic where the endpoint gives a signal, because
(a) the deep probe corrects it within minutes, and (b) the engine's repair loop
+ downgrade ladders gracefully absorb an over-optimistic seed (a native call
that fails → repair → ladder down for that turn). A conservative seed would
defeat the purpose (the user waits on the floor for no reason).

| Seed field | Source | Rule |
|---|---|---|
| `context_window` | `show_model().context_length` | advertised window (Ollama only) |
| `honest_context` | `show_model().num_ctx_configured` or `context_length` | the server's REAL usable window (num_ctx if the server pins it — the truncation trap), else the advertised window capped conservatively; never optimistic beyond what the server will process. Fall back to the default 4096 if not Ollama / show fails. |
| `tool_protocols` | `detect().native_tools` | detected → `{"native": 0.95}` (clears the ladder threshold → native tool-calling); else `{}` (text-protocol floor). This is **beyond** `as_priors` (which stays at 0.5/floor) — the whole point of a *usable* seed. |
| `edit_formats` | proxy: native-capable | native detected → `{"search_replace": 0.85}` (safe middle rung, works on large files); else `{}` (whole-file floor). |
| `source` | — | `"seeded"` (new field, see below) |
| `probed_at` | — | `None` — the profile is still "unprobed", so the deep probe still runs |

Resilience: every introspection call is best-effort — `show_model` failure →
keep the context default; `detect` failure → keep the floor tool/edit ladders.
`seed_profile` NEVER raises and completes in ~1-2s (metadata `/api/show` is
instant; `detect` is ≤5 tiny `max_tokens=8` chat calls). The api_key never leaks
(detect already guarantees this).

## Components

1. **`CapabilityProfile.source: str = "default"`** — additive field, values
   `"default" | "seeded" | "probed"`. Set by `seed_profile` → `"seeded"`, by
   `run_probes` → `"probed"`. Report card / doctor show it so the user knows
   whether values are introspected guesses or measurements. *(profile.py; note
   the additive field in CONTRACTS §5 same commit.)*

2. **`envelope/seed.py`** — `async seed_profile(provider, *, model_id,
   transport=None) -> CapabilityProfile`. Pulls `base_url`/`api_key` off the
   provider for `detect(...)`; calls `provider.show_model(model_id)` when the
   provider exposes it (Ollama). Assembles the seed per the table above.
   Testable: inject a `MockTransport` for `detect` and an OllamaProvider on a
   `MockTransport` (or a fake `show_model`) for the context. *(new file + tests.)*

3. **`base` plumbing** — `probe_model` / `envelopecmd.probe_and_swap` accept an
   optional `base: CapabilityProfile` passed through to `run_probes`, so the deep
   probe **refines** the seed (an introspected `honest_context` survives a
   CtxHonesty probe failure instead of collapsing to 4096). *(suite.py,
   envelopecmd.py.)*

4. **Config** — `EnvelopeSettings.instant_seed: bool = True`. *(settings.py.)*

5. **App wiring** — `from_settings` flags an unprobed model; `on_mount` (when
   `instant_seed` and unprobed): schedule a **seed** worker (hot-swap + a note
   like "seeded from introspection: ctx 32768, native tools ✓ — measuring in the
   background"), then a **deep-probe** worker (`base=` the seed, hot-swap + the
   report card). Status bar reflects seed → measured. *(app.py.)*

6. **Display** — `render_report_card` labels the `source` (seeded = provisional,
   probed = measured); `doctor` notes a seeded-but-unmeasured state. *(runner.py
   render, cli.py.)*

## Swarm plan (doer → validator at every step)

- **Wave 1 — seed core.** DOER builds `CapabilityProfile.source`, `seed.py`, and
  the `base` plumbing + tests. VALIDATOR verifies by execution: the seed is
  usable (native when detected, real context), honest (never overruns the
  server window), fast, resilient (each introspection failure degrades to floor,
  never raises), and `base`-refine works; no regressions.
- **Wave 2 — wiring (parallel).** DOER-A wires the app seed-then-probe flow
  (owns `app.py`). DOER-B wires config + report-card/doctor display (owns
  `settings.py`, `runner.py` render, `cli.py`). VALIDATOR verifies the
  end-to-end seed→probe hot-swaps, the display honesty, and config toggling.
- **Final.** Orchestrator e2e proof (MockProvider: seed → first turn uses the
  seed → deep probe refines → hot-swap), full-suite + coverage validation, README
  moonshot updated, push.

Acceptance: pointing IronCore at a fresh Ollama model, the first turn runs with
the model's real context window and native tool-calling within ~1s (not the 4k
text floor), and the measured profile hot-swaps in behind it — with no blocking
wait.
