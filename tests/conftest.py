from __future__ import annotations

import pytest

from retail_agent.config import (
    AgentConfig,
    BigQueryConfig,
    ModelConfig,
    ObservabilityConfig,
    QdrantConfig,
)


@pytest.fixture
def test_config(tmp_path):
    return AgentConfig(
        bigquery=BigQueryConfig(
            dataset="bigquery-public-data.thelook_ecommerce",
            allowed_tables=["orders", "order_items", "products", "users"],
            max_result_rows=25,
        ),
        retrieval=QdrantConfig(
            url="http://localhost:6333",
            collection="test_trios",
        ),
        model=ModelConfig(embedding_provider="hash"),
        observability=ObservabilityConfig(log_path=tmp_path / "agent-runs.jsonl"),
    )
