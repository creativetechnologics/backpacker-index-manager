"""LLM provider adapters for the initial fill pipeline.

Each provider implements the same minimal surface so the orchestrator and
web server can treat them uniformly.
"""
from __future__ import annotations

from typing import Any, Protocol


class Provider(Protocol):
    """Minimal LLM call interface used by lane workers.

    Implementations should:
      - return parsed JSON content + a usage dict
      - raise ``ProviderError`` on transport / auth / parse failures
    """

    name: str
    model: str

    def call(self, prompt: str) -> tuple[dict[str, Any], dict[str, int]]:
        ...


class ProviderError(RuntimeError):
    """Raised when a provider call fails. Lane worker treats this as retryable."""


class RateLimitError(ProviderError):
    """Raised when a provider returns 429. Carries ``retry_after_s`` so the
    lane worker can wait the right amount before retrying.

    Some providers embed the suggested wait in the error body
    (e.g. OpenRouter returns ``retry_after_seconds``); others just send
    a 429 and we fall back to a default delay.
    """

    def __init__(self, message: str, retry_after_s: float = 30.0):
        super().__init__(message)
        self.retry_after_s = max(1.0, float(retry_after_s))


def import_provider(name: str) -> type[Provider]:
    """Lazy import by provider name to avoid pulling in unused deps.

    The ``openai-compatible`` family covers any gateway that speaks
    OpenAI's chat completions spec (opencode-zen, nvidia, minimax,
    and any future additions). The class picks the right env var
    based on the ``provider_name`` kwarg the lane worker passes.
    """
    from . import deepseek_direct, openrouter, opencode_go, local, openai_compatible

    registry: dict[str, type[Provider]] = {
        "deepseek-direct": deepseek_direct.DeepSeekDirectClient,
        "openrouter": openrouter.OpenRouterClient,
        "opencode-go": opencode_go.OpenCodeGoClient,
        "local": local.LocalClient,
        "opencode-zen": openai_compatible.OpenAICompatibleClient,
        "nvidia": openai_compatible.OpenAICompatibleClient,
        "minimax": openai_compatible.OpenAICompatibleClient,
    }
    if name not in registry:
        raise ValueError(f"Unknown provider: {name}. Available: {sorted(registry)}")
    return registry[name]
