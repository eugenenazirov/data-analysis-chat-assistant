import json

import pytest
from qdrant_client import QdrantClient

from retail_agent.embeddings import HashingEmbedder
from retail_agent.golden_store import GoldenStore
from retail_agent.models import GoldenTrio
from retail_agent.observability import EventLogger


def test_golden_store_indexes_and_searches(test_config, tmp_path):
    logger = EventLogger(tmp_path / "runs.jsonl")
    store = GoldenStore(test_config, HashingEmbedder(size=16), logger)
    store.client = QdrantClient(":memory:")
    trios = [
        GoldenTrio(
            id="revenue",
            question="monthly revenue by category",
            sql="SELECT category, revenue FROM table LIMIT 10",
            analyst_report="Rank categories by revenue.",
            tags=["revenue"],
        ),
        GoldenTrio(
            id="returns",
            question="products with high return risk",
            sql="SELECT product, return_rate FROM table LIMIT 10",
            analyst_report="Find high return rates.",
            tags=["returns"],
        ),
    ]

    assert store.index(trios, recreate=True) == 2
    results = store.search("show revenue by category", trace_id="trace", limit=1)

    assert len(results) == 1
    assert results[0].id in {"revenue", "returns"}


def test_golden_store_loads_seed_file_and_recreates_collection(test_config, tmp_path):
    logger = EventLogger(tmp_path / "runs.jsonl")
    store = GoldenStore(test_config, HashingEmbedder(size=8), logger)
    store.client = QdrantClient(":memory:")
    seed_path = tmp_path / "trios.jsonl"
    seed_path.write_text(
        json.dumps(
            {
                "id": "revenue",
                "question": "Revenue?",
                "sql": "SELECT 1",
                "analyst_report": "Revenue was stable.",
                "tags": ["revenue"],
            }
        )
        + "\n\n",
        encoding="utf-8",
    )

    trios = store.load_seed_trios(seed_path)

    assert len(trios) == 1
    assert store.index([]) == 0
    assert store.index(trios) == 1
    assert store.index(trios, recreate=True) == 1


def test_golden_store_wait_until_ready_returns_after_success(test_config, tmp_path):
    class ReadyClient:
        def get_collections(self):
            return []

    store = GoldenStore(
        test_config,
        HashingEmbedder(size=8),
        EventLogger(tmp_path / "runs.jsonl"),
    )
    store.client = ReadyClient()

    store.wait_until_ready(timeout_seconds=1)


def test_golden_store_wait_until_ready_raises_last_error(
    test_config, tmp_path, monkeypatch
):
    class FailingClient:
        def get_collections(self):
            raise ConnectionError("not ready")

    times = iter([0.0, 0.1, 2.0])
    monkeypatch.setattr("retail_agent.golden_store.time.time", lambda: next(times))
    monkeypatch.setattr("retail_agent.golden_store.time.sleep", lambda seconds: None)
    store = GoldenStore(
        test_config,
        HashingEmbedder(size=8),
        EventLogger(tmp_path / "runs.jsonl"),
    )
    store.client = FailingClient()

    with pytest.raises(RuntimeError, match="not ready"):
        store.wait_until_ready(timeout_seconds=1)
