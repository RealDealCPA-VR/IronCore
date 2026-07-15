"""Model providers.

One OpenAI-compatible client covers Ollama, vLLM, llama.cpp server,
LM Studio, OpenRouter, Together, and Groq (SPEC.md #8). Provider-specific
extras (Ollama model discovery, context-length introspection, grammar
support detection) layer on top.
"""

from ironcore.providers.base import (
    CompletionResult,
    Message,
    Provider,
    SamplingPolicy,
    StreamEvent,
    ToolCall,
)
from ironcore.providers.mock import (
    MalformedToolJSON,
    MockProvider,
    RaiseError,
    TimeoutFailure,
    Truncate,
)
from ironcore.providers.registry import ProviderRegistry

__all__ = [
    "CompletionResult",
    "MalformedToolJSON",
    "Message",
    "MockProvider",
    "Provider",
    "ProviderRegistry",
    "RaiseError",
    "SamplingPolicy",
    "StreamEvent",
    "TimeoutFailure",
    "ToolCall",
    "Truncate",
]
