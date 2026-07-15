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
from ironcore.providers.mock import MockProvider

__all__ = [
    "CompletionResult",
    "Message",
    "MockProvider",
    "Provider",
    "SamplingPolicy",
    "StreamEvent",
    "ToolCall",
]
