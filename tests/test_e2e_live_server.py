"""End-to-end proof: the phase-1/2 provider stack against a REAL HTTP server.

Everything else in the suite uses httpx.MockTransport. This module boots an
actual stdlib HTTP server on a loopback socket and drives the real network
path — TCP connect, auth headers, SSE framing, 429 retries — through
OpenAICompatProvider, OllamaProvider, and detect(). It stays offline-first:
the server lives in-process on 127.0.0.1, so it runs anywhere CI does.
"""

import asyncio
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import Message, SamplingPolicy
from ironcore.providers.detect import as_priors, detect
from ironcore.providers.ollama import OllamaProvider
from ironcore.providers.openai_compat import OpenAICompatProvider, ProviderError

API_KEY = "proof-key-DO-NOT-LEAK"

_SSE_BODY = b"".join(
    b"data: " + json.dumps(chunk).encode() + b"\n\n"
    for chunk in [
        {"choices": [{"index": 0, "delta": {"content": "Checking "}}]},
        {"choices": [{"index": 0, "delta": {"content": "the file."}}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_proof",
                                "function": {"name": "read_file", "arguments": '{"pa'},
                            }
                        ]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": 'th": "src/ap'}}]
                    },
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'p.py"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
) + b"data: [DONE]\n\n"

_COMPLETION = {
    "id": "cmpl-proof",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "pong from a real socket"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

_SHOW = {
    "details": {"quantization_level": "Q4_K_M", "family": "llama"},
    "model_info": {"llama.context_length": 8192},
    "parameters": 'num_ctx 4096\nstop "</s>"',
}


class _ProofServer(ThreadingHTTPServer):
    """Records what really arrived over the wire, for assertions."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.auth_headers: list[str] = []
        self.chat_bodies: list[dict] = []
        self.retry_hits = 0


class _ProofHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output clean
        pass

    def _send(self, status: int, payload: bytes, content_type: str, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj, status: int = 200, extra=None):
        self._send(status, json.dumps(obj).encode(), "application/json", extra)

    def do_GET(self):
        if self.path == "/api/version":
            self._json({"version": "0.9.9-proof"})
        elif self.path == "/api/tags":
            self._json(
                {
                    "models": [
                        {
                            "name": "proof-model:7b",
                            "size": 4_000_000_000,
                            "modified_at": "2026-07-15T00:00:00Z",
                        }
                    ]
                }
            )
        elif self.path == "/v1/models":
            self._json({"data": [{"id": "proof-model:7b", "owned_by": "library"}]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/show":
            self._json(_SHOW)
            return
        if self.path != "/v1/chat/completions":
            self._json({"error": "not found"}, 404)
            return

        server: _ProofServer = self.server  # type: ignore[assignment]
        server.auth_headers.append(self.headers.get("Authorization", ""))
        server.chat_bodies.append(body)

        if body.get("model") == "retry-model" and server.retry_hits == 0:
            server.retry_hits += 1
            self._json({"error": "slow down"}, 429, extra={"Retry-After": "0"})
            return
        if body.get("stream"):
            self._send(200, _SSE_BODY, "text/event-stream")
        else:
            self._json(_COMPLETION)


@pytest.fixture()
def live_server():
    server = _ProofServer(("127.0.0.1", 0), _ProofHandler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def _run(coro):
    return asyncio.run(coro)


def test_complete_over_real_socket(live_server):
    server, base_url = live_server
    provider = OpenAICompatProvider(base_url, api_key=API_KEY, model="proof-model:7b")

    async def go():
        try:
            return await provider.complete([Message(role="user", content="ping")])
        finally:
            await provider.close()

    result = _run(go())
    assert result.message.content == "pong from a real socket"
    assert result.usage["total_tokens"] == 15
    assert result.finish_reason == "stop"
    assert server.auth_headers[-1] == f"Bearer {API_KEY}"  # the key really crossed the wire


def test_stream_reassembles_fragmented_tool_call_over_real_socket(live_server):
    _, base_url = live_server
    provider = OpenAICompatProvider(base_url, api_key=API_KEY, model="proof-model:7b")

    async def go():
        try:
            return [
                event
                async for event in provider.stream([Message(role="user", content="read it")])
            ]
        finally:
            await provider.close()

    events = _run(go())
    text = "".join(e.text for e in events if e.kind == "text")
    assert text == "Checking the file."
    calls = [e.tool_call for e in events if e.kind == "tool_call"]
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "src/app.py"}  # split across 3 SSE chunks
    assert events[-1].kind == "done"
    assert events[-1].data["finish_reason"] == "tool_calls"


def test_retry_after_429_over_real_socket(live_server):
    server, base_url = live_server
    provider = OpenAICompatProvider(base_url, api_key=API_KEY, model="retry-model")

    async def go():
        try:
            return await provider.complete([Message(role="user", content="ping")])
        finally:
            await provider.close()

    result = _run(go())
    assert result.message.content == "pong from a real socket"
    assert server.retry_hits == 1  # the 429 really happened, then the retry succeeded


def test_ollama_discovery_show_and_ctx_warning_over_real_socket(live_server):
    _, base_url = live_server
    provider = OllamaProvider(base_url, api_key=API_KEY, model="proof-model:7b")

    async def go():
        try:
            models = await provider.discover_models()
            details = await provider.show_model("proof-model:7b")
            warning = await provider.check_context("proof-model:7b", 8192)
            return models, details, warning
        finally:
            await provider.close()

    models, details, warning = _run(go())
    assert [m.name for m in models] == ["proof-model:7b"]
    assert details.context_length == 8192
    assert details.quantization == "Q4_K_M"
    assert details.num_ctx_configured == 4096  # parsed out of the Modelfile blob
    assert warning is not None  # 4096 configured < 8192 wanted -> the MODELS.md trap, caught


def test_detect_over_real_socket_and_priors_stay_on_the_floor(live_server):
    _, base_url = live_server

    features = _run(detect(base_url, api_key=API_KEY, model="proof-model:7b"))
    assert features.server_hint == "ollama"  # /api/version answered
    assert features.native_tools is True  # server accepted the tools param
    assert features.grammar is False  # llama.cpp knob on an ollama server: ignored
    assert features.logprobs is False  # body-verified: no logprobs in the response

    priors = as_priors(features)
    profile = CapabilityProfile(model_id="proof-model:7b", tool_protocols=priors)
    assert profile.recommended_tool_protocol() == "text_protocol"  # priors never beat probes


def test_dead_endpoint_error_never_leaks_the_key():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()  # nothing listens here now

    provider = OpenAICompatProvider(
        f"http://127.0.0.1:{dead_port}/v1",
        api_key=API_KEY,
        model="m",
        connect_timeout=0.5,
    )

    async def go():
        try:
            await provider.complete(
                [Message(role="user", content="ping")],
                sampling=SamplingPolicy(retries=0),
            )
        finally:
            await provider.close()

    with pytest.raises(ProviderError) as excinfo:
        _run(go())
    seen = repr(excinfo.value) + str(excinfo.value) + repr(excinfo.value.__cause__)
    assert API_KEY not in seen
