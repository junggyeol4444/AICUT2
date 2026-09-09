"""Reasoning providers (18장 boundary)."""

from __future__ import annotations

from typing import Any

from aicut.errors import ProviderError
from aicut.llm.base import Producer

__all__ = ["PRODUCERS", "Producer", "get_producer"]


#: Every provider that can answer the judgement tasks. 18장 says where the line
#: between program and AI is, not which machine the AI runs on, and 20.1's stack
#: names no model service at all - so a local one is as much in-spec as a remote
#: one, and is the only option for an operator who will not send hours of their
#: own broadcast to somebody else's server.
PRODUCERS = ("mock", "anthropic", "ollama")


def get_producer(name: str = "mock", **kwargs: Any) -> Producer:
    """Build a producer by name.

    ``mock`` runs offline and decides nothing - it exists so the pipeline can be
    exercised without a model, and its answers are not judgements.
    ``anthropic`` needs a key; ``ollama`` needs a model server, local by default.
    """
    if name == "mock":
        from aicut.llm.mock import MockProducer

        return MockProducer()
    if name == "anthropic":
        from aicut.llm.anthropic_provider import AnthropicProducer

        return AnthropicProducer(**kwargs)
    if name == "ollama":
        from aicut.llm.ollama_provider import OllamaProducer

        return OllamaProducer(**kwargs)
    raise ProviderError(
        f"unknown producer {name!r}; expected one of {', '.join(PRODUCERS)}"
    )
