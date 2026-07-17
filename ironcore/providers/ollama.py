"""Ollama-native extras on top of the OpenAI-compatible client (IC-203).

Ollama serves two API surfaces from one server: the /v1 OpenAI dialect
(handled entirely by the parent class) and its native /api/* endpoints,
which live at the server ROOT — /api is a sibling of /v1, never nested
under it. ``api_root`` derives that root by stripping a trailing /v1
from base_url; all /api/* requests are built from it, never by
concatenating onto base_url.

What the native surface adds (SPEC §8.2, MODELS.md §7):

* discover_models(): GET /api/tags — locally pulled models with size and
  mtime. list_models() prefers these names and falls back to the
  parent's /v1/models on any failure, so pointing this provider at a
  non-Ollama endpoint degrades gracefully instead of breaking listing.
* show_model(): POST /api/show — the model's TRUE context window
  (model_info keys like "llama.context_length"), quantization level and
  family, plus the server-configured num_ctx parsed from the parameters
  text blob. The server-side num_ctx default may be far below the
  model's window; check_context() turns that mismatch into a human
  warning before long prompts get silently truncated.
* keep_alive: when set (default "10m"), injected into every chat body so
  interactive sessions don't reload weights between turns.

ModelDetails field names are consumed by the envelope (IC-601) — do not
rename. Error rules match the parent: ProviderError only, every message
routed through self._redact / self._describe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from ironcore.providers.base import Message, SamplingPolicy
from ironcore.providers.openai_compat import (
    _RETRY_STATUSES,
    _TRANSPORT_ERRORS,
    OpenAICompatProvider,
    ProviderError,
    _backoff_delay,
)

#: the parameters blob is Modelfile text: one "key value" pair per line
_NUM_CTX_RE = re.compile(r"^\s*num_ctx\s+(\d+)\s*$", re.MULTILINE)


@dataclass
class ModelInfo:
    """One locally pulled model, as reported by /api/tags."""

    name: str
    size_bytes: int | None = None
    #: raw server timestamp string — callers parse it if they care
    modified_at: str | None = None


@dataclass
class ModelDetails:
    """Introspection from /api/show. Field names are load-bearing:
    the envelope (IC-601) consumes them verbatim."""

    #: the model's true window, from model_info "<arch>.context_length"
    context_length: int | None = None
    #: details.quantization_level, e.g. "Q4_K_M"
    quantization: str | None = None
    #: details.family, e.g. "llama"
    family: str | None = None
    #: server-configured num_ctx from the parameters blob (may be far
    #: below context_length — that gap is the MODELS.md §7 trap)
    num_ctx_configured: int | None = None
    #: the /api/show "capabilities" array (modern Ollama reports e.g.
    #: ["completion", "vision"]); [] when absent — never a guess (MS-6)
    capabilities: list[str] = field(default_factory=list)


def _as_int(value: Any) -> int | None:
    """Strictly-an-int filter (bool is an int subclass; reject it)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _find_context_length(model_info: Any) -> int | None:
    """Hunt model_info for the architecture-prefixed context length —
    Ollama nests it as e.g. "llama.context_length"; first int wins."""
    if not isinstance(model_info, dict):
        return None
    for key, value in model_info.items():
        if not isinstance(key, str):
            continue
        if key == "context_length" or key.endswith(".context_length"):
            found = _as_int(value)
            if found is not None:
                return found
    return None


class OllamaProvider(OpenAICompatProvider):
    """See module docstring."""

    name = "ollama"

    def __init__(
        self,
        base_url: str,
        api_key: str = "ironcore-local",
        model: str = "",
        *,
        keep_alive: str | None = "10m",
        **kwargs: Any,
    ) -> None:
        # Ollama's OpenAI dialect lives under /v1. A bare root URL
        # (http://host:11434) would make every chat call 404 while /api/*
        # still works — a confusing half-broken provider. This subclass
        # knows the server layout, so it completes the URL itself.
        if not base_url.rstrip("/").endswith("/v1"):
            base_url = base_url.rstrip("/") + "/v1"
        super().__init__(base_url, api_key=api_key, model=model, **kwargs)
        self.keep_alive = keep_alive

    # ------------------------------------------------------------ transport

    @property
    def api_root(self) -> str:
        """base_url with a trailing /v1 (or /v1/) stripped: native /api/*
        endpoints live at the server root, beside /v1 — never under it."""
        root = self.base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        return root

    async def _send_api(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        """The parent's _send_with_retries, re-anchored at api_root (the
        parent concatenates base_url + path, which would nest /api under
        /v1). Same retry statuses, backoff, and redaction rules."""
        if retries is None:
            retries = SamplingPolicy().retries
        attempts = max(retries, 0) + 1
        failure = "no attempts made"
        for attempt in range(attempts):
            retry_after: str | None = None
            try:
                request = self._client.build_request(method, self.api_root + path, json=json_body)
                response = await self._client.send(request)
            except _TRANSPORT_ERRORS as exc:
                failure = self._describe(exc)
            else:
                if response.status_code < 400:
                    return response
                body = (await response.aread()).decode("utf-8", "replace")
                await response.aclose()
                snippet = self._redact(" ".join(body.split())[:200])
                failure = f"HTTP {response.status_code} from {path}: {snippet}"
                if response.status_code not in _RETRY_STATUSES:
                    raise ProviderError(failure)
                retry_after = response.headers.get("Retry-After")
            if attempt + 1 < attempts:
                await self._sleep(_backoff_delay(attempt, retry_after))
        raise ProviderError(f"request failed after {attempts} attempt(s): {failure}")

    # -------------------------------------------------------------- request

    def _request_body(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        sampling: SamplingPolicy,
        *,
        stream: bool,
        response_format: dict | None = None,
        extra_body: dict | None = None,
    ) -> dict[str, Any]:
        body = super()._request_body(
            messages,
            tools,
            sampling,
            stream=stream,
            response_format=response_format,
            extra_body=extra_body,
        )
        if self.keep_alive is not None:
            # keep weights resident between interactive turns (SPEC §8.2)
            body["keep_alive"] = self.keep_alive
        return body

    # ------------------------------------------------------------ discovery

    async def discover_models(self) -> list[ModelInfo]:
        """GET /api/tags -> the locally pulled models. A 404, connection
        failure, or non-JSON body raises ProviderError with a hint that
        the endpoint may not be an Ollama server at all."""
        try:
            response = await self._send_api("GET", "/api/tags")
        except ProviderError as exc:
            raise ProviderError(f"/api/tags unavailable (not an Ollama endpoint?): {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"malformed JSON from /api/tags (not an Ollama endpoint?): {self._describe(exc)}"
            ) from exc
        entries = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise ProviderError("/api/tags carried no model list (not an Ollama endpoint?)")
        infos: list[ModelInfo] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("model")  # newer servers send both
            if not isinstance(name, str) or not name:
                continue
            modified = entry.get("modified_at")
            infos.append(
                ModelInfo(
                    name=name,
                    size_bytes=_as_int(entry.get("size")),
                    modified_at=modified if isinstance(modified, str) else None,
                )
            )
        return infos

    async def list_models(self) -> list[str]:
        """Prefer native /api/tags names; on any failure fall back to the
        parent's OpenAI /models path so non-Ollama endpoints still list."""
        try:
            return [info.name for info in await self.discover_models()]
        except ProviderError:
            return await super().list_models()

    async def show_model(self, name: str) -> ModelDetails:
        """POST /api/show -> ModelDetails. Absent fields are None, never
        a guess — the envelope treats unknowns conservatively."""
        try:
            response = await self._send_api("POST", "/api/show", json_body={"model": name})
        except ProviderError as exc:
            raise ProviderError(
                f"/api/show failed for {name!r} (not an Ollama endpoint?): {exc}"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"malformed JSON from /api/show: {self._describe(exc)}") from exc
        if not isinstance(payload, dict):
            raise ProviderError("/api/show returned no object")
        details = payload.get("details")
        details = details if isinstance(details, dict) else {}
        quantization = details.get("quantization_level")
        family = details.get("family")
        num_ctx: int | None = None
        parameters = payload.get("parameters")
        if isinstance(parameters, str):
            match = _NUM_CTX_RE.search(parameters)
            if match:
                num_ctx = int(match.group(1))
        raw_caps = payload.get("capabilities")
        capabilities = (
            [cap for cap in raw_caps if isinstance(cap, str)]
            if isinstance(raw_caps, list)
            else []
        )
        return ModelDetails(
            context_length=_find_context_length(payload.get("model_info")),
            quantization=quantization if isinstance(quantization, str) else None,
            family=family if isinstance(family, str) else None,
            num_ctx_configured=num_ctx,
            capabilities=capabilities,
        )

    async def check_context(self, name: str, wanted_ctx: int) -> str | None:
        """Human warning when the model cannot actually serve wanted_ctx
        tokens: the weights' window is smaller, or the server's configured
        num_ctx silently truncates below it (MODELS.md §7). None when fine
        or when /api/show reported nothing to judge by."""
        details = await self.show_model(name)
        problems: list[str] = []
        if details.context_length is not None and details.context_length < wanted_ctx:
            problems.append(f"the model's window is {details.context_length} tokens")
        if details.num_ctx_configured is not None and details.num_ctx_configured < wanted_ctx:
            problems.append(f"the server's num_ctx is configured to {details.num_ctx_configured}")
        if not problems:
            return None
        return (
            f"{name}: requested context of {wanted_ctx} tokens exceeds capacity — "
            + " and ".join(problems)
            + "; longer prompts will be silently truncated."
        )
