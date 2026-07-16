# Implementation Plan — Guided Decoding (the real `strict_json` rung)

> **STATUS: SHIPPED (2026-07-16).** Built via a doer→validator swarm; both waves
> validated SHIP by execution. The `Provider` `response_format`/`extra_body` seam,
> `core/guided.py` (schema + fragment + parser with the `done` action), the engine
> `strict_json` path, and the guided-measuring probe are live and pinned by
> `tests/test_guided.py` + `tests/test_e2e_guided.py`. Plan retained as the design
> record.

> Make the middle rung of the tool-protocol ladder a
> genuine capability: when the envelope routes a mid-tier model to `strict_json`,
> constrain the server's generation to a JSON schema so the model emits a
> **guaranteed well-formed** tool call — not best-effort native function-calling
> it may not sustain.

## Problem

The ladder is `native → strict_json → text_protocol (IRONCALL floor)`. But the
engine only branches `text_protocol` vs "everything else" (`engine.py`
`protocol == "text_protocol"`), so **`strict_json` collapses to the native
path** — the same unconstrained function-calling. A model measured in the
0.90–0.95 band (good, not great) gets native behavior with no guarantee, and the
`Provider` has no seam to request server-side constrained decoding
(`response_format` / json-schema / GBNF / vLLM `guided_json`). The rung is
decorative.

## What guided decoding does

Modern local servers can *force* output to match a JSON schema:

- **OpenAI `response_format`** — `{"type":"json_object"}` (valid JSON) or
  `{"type":"json_schema","json_schema":{...}}` (a specific schema, "structured
  outputs"). Accepted by **vLLM, llama.cpp server, LM Studio, and recent Ollama
  via `/v1`** — the standard, portable form.
- **vLLM `guided_json` / `guided_grammar`**, **llama.cpp `grammar` (GBNF)** —
  server-specific body keys for the same effect (the `extra_body` escape hatch).

The `strict_json` rung will send `response_format` (a json-schema constraining
output to one tool call) so the model *cannot* emit a malformed call.

## Design

### The one-tool-call schema

`strict_json` constrains output to exactly one call:

```json
{"type":"object",
 "properties":{"tool":{"enum":[<tool names> , "done"]},
               "args":{"type":"object"}},
 "required":["tool","args"], "additionalProperties":false}
```

The `enum` guarantees a valid tool NAME; `args` an object — a structurally
well-formed call, every time. (Per-tool arg schemas via `oneOf` are a documented
future refinement; the enum+object form is portable across every guided backend.)

**The `done` pseudo-tool.** Because `response_format` forces JSON on *every*
turn, the model can't emit free text to finish. So the schema includes a `done`
option: `{"tool":"done","args":{"message":"<summary>"}}` ends the turn and shows
the message. This keeps the model fully constrained yet able to stop.

### The Provider seam (CONTRACTS §2 — additive, documented same commit)

Add optional keyword-only params to `Provider.complete`/`stream` (default `None`,
fully backward-compatible; `MockProvider` stays a drop-in and records them):

```python
response_format: dict | None = None   # OpenAI form; put in the request body
extra_body: dict | None = None        # server-specific knobs (guided_json/grammar)
```

`OpenAICompatProvider._request_body` merges both into the body when set
(`OllamaProvider` inherits via `super()._request_body`). No engine→provider
protocol string is passed — the engine decides *what* to constrain; the provider
only forwards the knob.

### The guided helper (`core/guided.py`, new, pure)

- `tool_call_response_format(tools: list[spec]) -> dict` — build the
  `{"type":"json_schema","json_schema":{"name":"ironcore_tool_call","strict":true,
  "schema":<one-call schema incl. "done">}}` object.
- `render_json_system_fragment(tools) -> str` — a short instruction + the tool
  catalog (name/description/params), teaching the model to emit
  `{"tool":...,"args":...}` and to finish with `done` (few-shot, like IRONCALL).
- `parse_guided_tool_call(text) -> GuidedParse(call, done, message, text, error)`
  — `json.loads` the constrained output; a `done` call → `done=True` + message;
  a real call → a `ToolCall`; unparseable (a server that ignored
  `response_format`) → a precise, repairable error string. Never raises.

### Engine `strict_json` path (`core/engine.py`)

`protocol = recommended_tool_protocol()`; add `guided = protocol == "strict_json"`.
When guided:

- COMPOSE: prepend `render_json_system_fragment(tools.specs())` to the system
  prompt (the model needs the tool docs; the schema only carries names).
- CALL: pass `response_format=tool_call_response_format(...)`, and **do NOT** pass
  native `tools` (the model emits the JSON directly). Accumulate streamed text.
- PARSE: `parse_guided_tool_call(full_text)`. `done` → end the turn with the
  message (evidence-based, like the model stopping). A call → GATE + EXECUTE as
  usual; feed the result back as a JSON `ironresult`-style message and loop.
  A malformed body → the repair loop (and, on repeated failure, ladder down to
  the text floor — which always works).

This is a genuine THIRD parse path alongside native `tool_calls` and IRONCALL.

### Close the loop: the probe measures the *guided* rung

The `strict_json` reliability the envelope routes on should reflect *guided*
decoding, not best-effort JSON. `ToolFormProbe`'s `strict_json` trials (and/or
`JsonStrictProbe`) send `response_format` so the measured score is how reliably
the model emits schema-conforming JSON *when constrained* — so the ladder only
routes to `strict_json` when guided decoding actually works on that server.
(`envelope/probe_tools.py`.)

### Fallback / robustness

The engine sends the `json_schema` form (the standard). If a server rejects it
(`400`), the request errors → repair → the engine ladders down to the text floor
for the rest of the turn (always works). A server that *ignores* `response_format`
returns best-effort JSON, which `parse_guided_tool_call` still parses when valid
and otherwise repairs. No hard failures.

## Swarm plan (doer → validator at each step)

- **Wave 1 (parallel).** DOER-A: the Provider seam (`base.py`, `openai_compat.py`,
  `mock.py`, CONTRACTS §2). DOER-B: `core/guided.py` (schema + fragment + parser).
  → VALIDATOR-1 verifies both by execution (the knob lands in the body; the schema
  is well-formed json-schema; the parser handles a call, `done`, and garbage).
- **Wave 2 (parallel).** DOER-C: the engine `strict_json` path (owns `engine.py`).
  DOER-D: the probe uses `response_format` (owns `probe_tools.py`). → VALIDATOR-2
  verifies a `strict_json`-profile turn sends `response_format`, executes the
  parsed tool, honors `done`, repairs a malformed body, and that the probe now
  measures guided reliability — with no regression to native/text paths.
- **Final.** Orchestrator e2e proof (a `strict_json` engine turn end-to-end
  against a MockProvider that records `response_format` and returns constrained
  JSON → the tool executes; `done` ends the turn), full-suite + coverage, README
  (move guided decoding from moonshots to shipped), push.

Acceptance: a model the envelope routes to `strict_json` is driven with
server-side `response_format` constraining every reply to a well-formed
`{"tool","args"}` object — guaranteed-parseable tool calls — and can still finish
a turn via `done`, with a clean ladder-down to the text floor if the server
can't constrain.
