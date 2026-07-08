from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from retail_agent import agent
from retail_agent.models import AnalysisReport, QueryResult, RetrievedTrio
from retail_agent.observability import EventLogger


class FakeGoldenStore:
    def __init__(self):
        self.calls = []
        self.results = [
            RetrievedTrio(
                id="trio_monthly_revenue_category",
                score=0.91,
                question="Which categories drove revenue?",
                sql="SELECT category, SUM(revenue) AS revenue FROM table GROUP BY category",
                analyst_report="Rank categories by revenue and include order count.",
                tags=["revenue", "category"],
            )
        ]

    def search(self, question: str, trace_id: str, limit: int = 3):
        self.calls.append({"question": question, "trace_id": trace_id, "limit": limit})
        return self.results


class FakeBigQueryRunner:
    def describe_allowed_tables(self) -> str:
        return "- `bigquery-public-data.thelook_ecommerce.order_items`: id INTEGER"


def test_run_question_prefetches_golden_knowledge_before_model(
    test_config, tmp_path, monkeypatch
):
    golden_store = FakeGoldenStore()
    prompts = []

    async def fake_run(prompt, *, deps, model):
        prompts.append(prompt)
        assert deps.golden_trios == golden_store.results
        deps.last_query_result = QueryResult(
            sql="SELECT category, revenue FROM safe_table LIMIT 10",
            rows=[{"category": "Outerwear", "revenue": 100}],
            total_rows=1,
        )
        return SimpleNamespace(
            output=AnalysisReport(
                question="Which categories drove revenue?",
                answer="Outerwear led revenue.",
            ),
            usage=lambda: None,
        )

    monkeypatch.setattr(agent.analysis_agent, "run", fake_run)

    report = asyncio.run(
        agent.run_question(
            "Which categories drove revenue?",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=golden_store,
            logger=EventLogger(tmp_path / "runs.jsonl"),
            user_id="manager_a",
        )
    )

    assert report.answer == "Outerwear led revenue."
    assert report.sql == "SELECT category, revenue FROM safe_table LIMIT 10"
    assert golden_store.calls
    assert golden_store.calls[0]["limit"] == 3
    assert "Golden Knowledge analyst precedents" in prompts[0]
    assert "trio_monthly_revenue_category" in prompts[0]
    assert "Rank categories by revenue" in prompts[0]

    events = [
        json.loads(line)
        for line in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    prepared = [
        event for event in events if event["event"] == "agent_golden_context_prepared"
    ]
    assert prepared[0]["ids"] == ["trio_monthly_revenue_category"]
