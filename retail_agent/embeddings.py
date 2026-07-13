"""Compatibility exports for retrieval embedding adapters."""

from retail_agent.infrastructure.retrieval.gemini_embeddings import (
    Embedder,
    GeminiEmbedder,
    HashingEmbedder,
)

__all__ = ["Embedder", "GeminiEmbedder", "HashingEmbedder"]
