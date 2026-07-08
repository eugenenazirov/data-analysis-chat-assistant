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
