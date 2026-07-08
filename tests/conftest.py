from __future__ import annotations

import pytest

from retail_agent.config import AgentConfig, BigQueryConfig, ObservabilityConfig, QdrantConfig


@pytest.fixture
def test_config(tmp_path):
    return AgentConfig(
        bigquery=BigQueryConfig(
            dataset="bigquery-public-data.thelook_ecommerce",
            allowed_tables=["orders", "order_items", "products", "users"],
            max_result_rows=25,
        ),
        qdrant=QdrantConfig(url="http://localhost:6333", collection="test_trios"),
        observability=ObservabilityConfig(log_path=tmp_path / "agent-runs.jsonl"),
    )
