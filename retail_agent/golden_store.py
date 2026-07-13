"""Compatibility exports for the Qdrant golden-example repository."""

from retail_agent.infrastructure.retrieval.qdrant_adapter import (
    GoldenStore,
    QdrantGoldenExampleRepository,
)

__all__ = ["GoldenStore", "QdrantGoldenExampleRepository"]
