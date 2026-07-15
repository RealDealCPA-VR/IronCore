"""IC-205 pins: one short request per feature with exactly one knob set,
server_hint heuristics (ollama /api/version at ROOT, vllm/llama.cpp via
/models owned_by), the silently-ignored-key logic for grammar/guided_json
under both hints, body-verified logprobs, dead endpoints -> all-False
without ever raising, and the as_priors dict shape (below every ladder
threshold).

Everything runs against httpx.MockTransport. Async pattern: asyncio.run
(pytest-asyncio is not a dependency of this repo).
"""

import asyncio
import json

import httpx

from ironcore.envelope.profile import TOOL_PROTOCOL_THRESHOLDS, CapabilityProfile
from ironcore.providers.detect import EndpointFeatures, as_priors, detect

BASE = "http://testserver/v1"
KNOBS = ("tools", "response_format", "grammar", "guided_json", "logprobs")

#: sentinel: the fake server omits the logprobs field entirely
OMIT = object()


class FakeServer:
    """Configurable OpenAI-compatible endpoint; records every request.

    reject: body keys that draw a 400 (a server that validates parameters).
    logprobs: value for choices[0]["logprobs"] when the request asks for
    logprobs — OMIT drops the field, None emits JSON null (OpenAI's
    "ignored the knob" shape).
    """

    def __init__(self, *, ollama_version=False, models=None, reject=(), logprobs=OMIT):
        self.ollama_version = ollama_version
        self.models = models
        self.reject = set(reject)
        self.logprobs = logprobs
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def chat_bodies(self) -> list[dict]:
        return [
            json.loads(request.content.decode())
            for request in self.requests
            if request.url.path == "/v1/chat/completions"
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/api/version":
            if self.ollama_version:
                return httpx.Response(200, json={"version": "0.9.9"})
            return httpx.Response(404, text="not found")
        if request.method == "GET" and path == "/v1/models":
            if self.models is not None:
                return httpx.Response(200, json={"object": "list", "data": self.models})
            return httpx.Response(404, text="not found")
        assert request.method == "POST" and path == "/v1/chat/completions", path
        body = json.loads(request.content.decode())
        rejected = self.reject.intersection(body)
        if rejected:
            return httpx.Response(
                400, json={"error": f"unknown parameter: {sorted(rejected)[0]}"}
            )
        choice = {
            "index": 0,
            "message": {"role": "assistant", "content": "pong"},
            "finish_reason": "stop",
        }
        if body.get("logprobs") and self.logprobs is not OMIT:
            choice["logprobs"] = self.logprobs
        return httpx.Response(200, json={"choices": [choice]})


def run_detect(server: FakeServer) -> EndpointFeatures:
    return asyncio.run(
        detect(
            BASE + "/",  # trailing slash on purpose: detect must normalize
            api_key="sk-unit-test",
            model="test-model",
            transport=server.transport(),
        )
    )


# --- request shape -----------------------------------------------------------


def test_each_feature_is_one_short_request_with_exactly_one_knob():
    server = FakeServer()
    run_detect(server)

    bodies = server.chat_bodies()
    assert len(bodies) == 5  # one request per feature, no more
    for body in bodies:
        assert body["model"] == "test-model"
        assert body["messages"] == [{"role": "user", "content": "ping"}]
        assert body["max_tokens"] <= 8
        assert sum(1 for knob in KNOBS if knob in body) == 1  # exactly one knob each
    sent = sorted(knob for body in bodies for knob in KNOBS if knob in body)
    assert sent == sorted(KNOBS)  # every feature probed exactly once

    posts = [r for r in server.requests if r.method == "POST"]
    assert all(str(r.url) == BASE + "/chat/completions" for r in posts)
    assert all(r.headers["authorization"] == "Bearer sk-unit-test" for r in posts)


# --- server_hint heuristics ---------------------------------------------------


def test_server_hint_ollama_via_api_version_at_server_root():
    server = FakeServer(ollama_version=True)
    assert run_detect(server).server_hint == "ollama"
    version_gets = [r for r in server.requests if r.url.path == "/api/version"]
    assert len(version_gets) == 1
    assert str(version_gets[0].url) == "http://testserver/api/version"  # ROOT, not under /v1


def test_server_hint_vllm_via_models_owned_by():
    server = FakeServer(models=[{"id": "qwen3", "object": "model", "owned_by": "vllm"}])
    assert run_detect(server).server_hint == "vllm"


def test_server_hint_llamacpp_via_models_owned_by():
    server = FakeServer(models=[{"id": "m.gguf", "owned_by": "llamacpp", "meta": {"n_ctx": 8192}}])
    assert run_detect(server).server_hint == "llama.cpp"


def test_server_hint_unknown_for_foreign_owned_by():
    server = FakeServer(models=[{"id": "gpt-x", "owned_by": "openai"}])
    assert run_detect(server).server_hint == "unknown"


# --- status-gated features ----------------------------------------------------


def test_native_tools_and_json_mode_present_on_200():
    features = run_detect(FakeServer())
    assert features.native_tools is True  # 200 = accepted; no call in the reply required
    assert features.json_mode is True


def test_rejected_knob_marks_only_that_feature_false():
    server = FakeServer(reject=("tools",), logprobs={"content": []})
    features = run_detect(server)
    assert features.native_tools is False  # 400 on this knob only
    assert features.json_mode is True
    assert features.logprobs is True


def test_rejected_response_format_marks_json_mode_false():
    assert run_detect(FakeServer(reject=("response_format",))).json_mode is False


# --- silently-ignored-key logic (grammar / guided_json), both hints -----------


def test_grammar_true_on_llamacpp_hint_and_200():
    server = FakeServer(models=[{"id": "m.gguf", "owned_by": "llamacpp"}])
    features = run_detect(server)
    assert features.grammar is True
    assert features.guided_json is False  # 200'd, but the hint is llama.cpp, not vllm


def test_guided_json_true_on_vllm_hint_and_200():
    server = FakeServer(models=[{"id": "qwen3", "owned_by": "vllm"}])
    features = run_detect(server)
    assert features.guided_json is True
    assert features.grammar is False  # 200'd, but the hint is vllm, not llama.cpp


def test_unknown_keys_accepted_on_ollama_prove_nothing():
    # ollama 200s unknown body keys — that must not count as support
    features = run_detect(FakeServer(ollama_version=True))
    assert features.server_hint == "ollama"
    assert features.grammar is False
    assert features.guided_json is False
    assert features.native_tools is True  # status-gated features still detect


def test_grammar_400_on_llamacpp_is_false():
    server = FakeServer(models=[{"id": "m", "owned_by": "llamacpp"}], reject=("grammar",))
    assert run_detect(server).grammar is False


# --- logprobs: verified in the body --------------------------------------------


def test_logprobs_requires_the_field_in_the_response_body():
    # 200 alone is not enough: choices[0] must actually carry logprobs
    assert run_detect(FakeServer(logprobs=OMIT)).logprobs is False
    # "logprobs": null is OpenAI's ignored-the-knob shape — also False
    assert run_detect(FakeServer(logprobs=None)).logprobs is False
    real = FakeServer(logprobs={"content": [{"token": "pong", "logprob": -0.01}]})
    assert run_detect(real).logprobs is True


# --- dead endpoints: never an exception ----------------------------------------


def test_dead_endpoint_returns_all_false_never_raises():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    features = asyncio.run(
        detect(BASE, api_key="sk-unit-test", model="m", transport=httpx.MockTransport(handler))
    )
    assert features == EndpointFeatures()  # the dataclass default IS the all-False shape
    assert features.server_hint == "unknown"


def test_timeout_returns_all_false_never_raises():
    def handler(request):
        raise httpx.ReadTimeout("read timed out")

    features = asyncio.run(
        detect(BASE, api_key="sk-unit-test", model="m", transport=httpx.MockTransport(handler))
    )
    assert features == EndpointFeatures()


def test_endpoint_dying_mid_detection_degrades_to_all_false():
    # hint GETs succeed, then the first chat probe hits a dead server:
    # partial results (the hint included) are discarded, not half-trusted
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"version": "1.0"})
        raise httpx.ConnectError("connection refused")

    features = asyncio.run(
        detect(BASE, api_key="sk-unit-test", model="m", transport=httpx.MockTransport(handler))
    )
    assert features == EndpointFeatures()


# --- as_priors ------------------------------------------------------------------


def test_priors_baseline_is_text_protocol_only():
    assert as_priors(EndpointFeatures()) == {"text_protocol": 0.5}


def test_priors_native_tools_adds_native():
    assert as_priors(EndpointFeatures(native_tools=True)) == {
        "text_protocol": 0.5,
        "native": 0.5,
    }


def test_priors_any_structured_output_feature_adds_strict_json():
    for field in ("json_mode", "grammar", "guided_json"):
        features = EndpointFeatures(**{field: True})
        assert as_priors(features) == {"text_protocol": 0.5, "strict_json": 0.5}


def test_priors_sit_below_every_ladder_threshold():
    features = EndpointFeatures(
        native_tools=True, json_mode=True, grammar=True, guided_json=True, logprobs=True
    )
    priors = as_priors(features)
    for proto, threshold in TOOL_PROTOCOL_THRESHOLDS.items():
        assert priors[proto] < threshold
    # the load-bearing consequence: priors alone must still land on the floor
    profile = CapabilityProfile(model_id="detected/unprobed", tool_protocols=priors)
    assert profile.recommended_tool_protocol() == "text_protocol"
