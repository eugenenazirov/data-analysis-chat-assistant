from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from qdrant_client import QdrantClient, models

from retail_agent.config import AgentConfig
from retail_agent.embeddings import Embedder
from retail_agent.models import GoldenTrio, RetrievedTrio
from retail_agent.observability import EventLogger


class GoldenStore:
    def __init__(self, config: AgentConfig, embedder: Embedder, logger: EventLogger):
        self.config = config
        self.embedder = embedder
        self.logger = logger
        api_key = config.retrieval.api_key
        self.client = QdrantClient(
            url=config.retrieval.url,
            api_key=api_key.get_secret_value() if api_key is not None else None,
            timeout=config.retrieval.timeout_seconds,
            check_compatibility=False,
        )

    def wait_until_ready(self, timeout_seconds: int = 30) -> None:
        deadline = time.time() + timeout_seconds
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                self.client.get_collections()
                return
            except Exception as exc:
                last_error = exc
                time.sleep(1)
        raise RuntimeError(f"Qdrant is not reachable: {last_error}")

    def load_seed_trios(self, path: Path) -> list[GoldenTrio]:
        trios: list[GoldenTrio] = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    trios.append(GoldenTrio.model_validate(json.loads(line)))
        return trios

    def index(self, trios: list[GoldenTrio], recreate: bool = False) -> int:
        if not trios:
            return 0
        first_vector = self.embedder.embed(trios[0].embedding_text())
        collection = self.config.retrieval.collection

        exists = self.client.collection_exists(collection)
        if recreate and exists:
            self.client.delete_collection(collection_name=collection)
            exists = False

        if not exists:
            self.client.create_collection(
                collection_name=collection,
                vectors_config=models.VectorParams(
                    size=len(first_vector), distance=models.Distance.COSINE
                ),
            )

        points = []
        for idx, trio in enumerate(trios):
            vector = first_vector if idx == 0 else self.embedder.embed(trio.embedding_text())
            points.append(
                models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"golden-trio:{trio.id}")),
                    vector=vector,
                    payload=trio.model_dump(mode="json"),
                )
            )
        self.client.upsert(collection_name=collection, points=points, wait=True)
        return len(points)

    def search(self, question: str, trace_id: str, limit: int = 3) -> list[RetrievedTrio]:
        vector = self.embedder.embed(question)
        response = self.client.query_points(
            collection_name=self.config.retrieval.collection,
            query=vector,
            limit=limit,
            with_payload=True,
        )
        points = getattr(response, "points", response)
        results: list[RetrievedTrio] = []
        for point in points:
            payload = point.payload or {}
            results.append(
                RetrievedTrio(
                    id=str(payload.get("id", point.id)),
                    score=float(point.score or 0.0),
                    question=str(payload.get("question", "")),
                    sql=str(payload.get("sql", "")),
                    analyst_report=str(payload.get("analyst_report", "")),
                    tags=list(payload.get("tags", [])),
                )
            )
        self.logger.event(
            trace_id,
            "golden_knowledge_retrieved",
            ids=[item.id for item in results],
            scores=[round(item.score, 4) for item in results],
        )
        return results
