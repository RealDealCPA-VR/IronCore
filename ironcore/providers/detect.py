"""Endpoint capability detection (IC-205).

Cheap, mechanical feature-detection for an OpenAI-compatible endpoint,
run once per (endpoint, model) before the probe suite: does the server
*accept* the request knobs the envelope ladders care about? Results feed
``CapabilityProfile.tool_protocols`` as PRIORS via :func:`as_priors` —
deliberately below every ladder threshold, so a detected-but-unprobed
model still lands on the text-protocol floor (docs/CONTRACTS.md #5).
The probe runners (IC-602..604) replace priors with measured scores.

Behavior (pinned by tests/providers/test_detect.py):

* One short chat request per feature (``max_tokens=8``, content "ping"),
  exactly one knob set per request, interpreted mechanically:
  - native_tools: trivial ``tools=[...]`` spec; True iff the server
    answers 2xx (it accepted the parameter — the reply need not contain
    an actual tool call).
  - json_mode: ``response_format={"type": "json_object"}``; True iff 2xx.
  - grammar: llama.cpp-style ``grammar`` body key (trivial GBNF); True
    iff 2xx AND server_hint == "llama.cpp" — other servers may silently
    ignore unknown body keys, so a 2xx alone proves nothing there.
  - guided_json: vLLM-style ``guided_json`` key; True iff 2xx AND
    server_hint == "vllm" (same silently-ignored-key logic).
  - logprobs: ``logprobs=true``; True iff 2xx AND choices[0] carries a
    non-null ``logprobs`` field (this one is verifiable in the body, so
    we verify it — OpenAI-schema servers emit null when they ignored it).
* server_hint is a cheap best-effort heuristic: GET {root}/api/version
  (Ollama serves native endpoints at the server ROOT, not under /v1)
  -> "ollama"; else GET {base}/models and sniff ``owned_by``/``meta``
  entry fields -> "vllm" / "llama.cpp"; else "unknown".
* Any 4xx/5xx -> that feature False. Connection errors / timeouts ->
  the all-False ``EndpointFeatures()`` with server_hint "unknown";
  ``detect()`` NEVER raises. No error text is logged, raised, or stored
  anywhere in this module, so the api_key cannot leak through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

#: same failure set openai_compat translates; here they mean "endpoint dead"
_TRANSPORT_ERRORS = (httpx.HTTPError, httpx.InvalidURL)

#: Prior score for any detected protocol — below every ladder threshold
#: (native 0.95 / strict_json 0.90) ON PURPOSE. Detection proves a knob is
#: *accepted*, not that the model *uses* it reliably; an unprobed model must
#: keep recommending the text-protocol floor (CONTRACTS.md #5, MODELS.md #3).
PRIOR_SCORE = 0.5

#: trivial tool spec: we only care whether the server accepts the parameter
_PING_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "ping",
            "description": "Reply with pong.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]
_PING_GRAMMAR = 'root ::= "pong"'  # trivial GBNF
_PING_SCHEMA: dict[str, Any] = {"type": "object"}  # trivial guided-decoding schema


@dataclass
class EndpointFeatures:
    """What one endpoint accepted. The default instance IS the all-False
    "nothing detected / endpoint unreachable" result."""

    native_tools: bool = False
    json_mode: bool = False
    grammar: bool = False
    guided_json: bool = False
    logprobs: bool = False
    server_hint: str = "unknown"  # "ollama" | "vllm" | "llama.cpp" | "unknown"


def _root(base: str) -> str:
    """Server root for native (non-/v1) routes like Ollama's /api/*."""
    return base[: -len("/v1")] if base.endswith("/v1") else base


async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    """GET url -> parsed JSON, or None on non-2xx / non-JSON / transport
    failure. Hint detection is best-effort: one dead route must not abort it."""
    try:
        response = await client.get(url)
    except _TRANSPORT_ERRORS:
        return None
    if not response.is_success:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _hint_from_models(payload: Any) -> str:
    """Sniff a GET {base}/models payload: vLLM stamps owned_by "vllm";
    llama.cpp server stamps owned_by "llamacpp" and attaches a "meta" blob."""
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return "unknown"
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        owner = str(entry.get("owned_by", "")).lower()
        if "vllm" in owner:
            return "vllm"
        if "llamacpp" in owner or "llama.cpp" in owner or "meta" in entry:
            return "llama.cpp"
    return "unknown"


async def _server_hint(client: httpx.AsyncClient, base: str) -> str:
    version = await _get_json(client, _root(base) + "/api/version")
    if isinstance(version, dict) and "version" in version:
        return "ollama"
    return _hint_from_models(await _get_json(client, base + "/models"))


async def _chat_probe(
    client: httpx.AsyncClient, base: str, model: str, knob: dict[str, Any]
) -> tuple[bool, Any]:
    """One minimal chat request with exactly one feature knob set ->
    (server accepted it, parsed response body or None). Any 4xx/5xx means
    not-accepted; transport errors propagate for detect() to catch."""
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        **knob,
    }
    response = await client.post(base + "/chat/completions", json=body)
    if not response.is_success:
        return False, None
    try:
        return True, response.json()
    except ValueError:
        return True, None  # accepted; body unusable for field checks


def _carries_logprobs(payload: Any) -> bool:
    """True when choices[0] actually carries logprobs content. OpenAI-schema
    servers emit "logprobs": null when they ignored the knob — that is False."""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        return False
    choice = choices[0]
    return isinstance(choice, dict) and choice.get("logprobs") is not None


async def detect(
    base_url: str,
    api_key: str = "ironcore-local",
    *,
    model: str,
    transport: httpx.AsyncBaseTransport | None = None,
    connect_timeout: float = 10.0,
    read_timeout: float = 120.0,
) -> EndpointFeatures:
    """Feature-detect one endpoint (module docstring has the per-feature
    heuristics). Never raises: a dead/unknown endpoint comes back as the
    all-False ``EndpointFeatures()`` with server_hint "unknown"."""
    base = base_url.rstrip("/")
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
        headers={"Authorization": f"Bearer {api_key}"},
        transport=transport,
    )
    try:
        hint = await _server_hint(client, base)
        native_ok, _ = await _chat_probe(client, base, model, {"tools": _PING_TOOLS})
        json_ok, _ = await _chat_probe(
            client, base, model, {"response_format": {"type": "json_object"}}
        )
        grammar_ok, _ = await _chat_probe(client, base, model, {"grammar": _PING_GRAMMAR})
        guided_ok, _ = await _chat_probe(client, base, model, {"guided_json": _PING_SCHEMA})
        logprobs_ok, logprobs_body = await _chat_probe(client, base, model, {"logprobs": True})
    except _TRANSPORT_ERRORS:
        # dead endpoint: partial results (hint included) are discarded — a
        # half-detected endpoint must not seed half-trusted priors
        return EndpointFeatures()
    finally:
        await client.aclose()
    return EndpointFeatures(
        native_tools=native_ok,
        json_mode=json_ok,
        # 2xx for an unknown body key proves nothing on servers that silently
        # ignore extras — only the matching server counts (module docstring)
        grammar=grammar_ok and hint == "llama.cpp",
        guided_json=guided_ok and hint == "vllm",
        logprobs=logprobs_ok and _carries_logprobs(logprobs_body),
        server_hint=hint,
    )


def as_priors(features: EndpointFeatures) -> dict[str, float]:
    """Detection results -> starting scores for CapabilityProfile.tool_protocols.

    These are PRIORS, not measurements: every value is PRIOR_SCORE, below all
    ladder thresholds on purpose, so ``recommended_tool_protocol()`` on a
    detected-but-unprobed model still returns the text-protocol floor
    (CONTRACTS.md #5). Probe runners (IC-602..604) overwrite them with
    measured reliabilities.
    """
    priors = {"text_protocol": PRIOR_SCORE}  # the floor always exists
    if features.native_tools:
        priors["native"] = PRIOR_SCORE
    if features.json_mode or features.grammar or features.guided_json:
        priors["strict_json"] = PRIOR_SCORE
    return priors
