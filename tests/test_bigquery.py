from __future__ import annotations

import json

import pytest

from retail_agent.bigquery import BigQueryRunner, QueryCostExceeded
from retail_agent.observability import EventLogger


class FakeJob:
    def __init__(
        self,
        *,
        total_bytes_processed: int = 0,
        total_bytes_billed: int = 0,
        rows: list[dict[str, object]] | None = None,
    ):
        self.total_bytes_processed = total_bytes_processed
        self.total_bytes_billed = total_bytes_billed
        self.rows = rows or []
        self.job_id = "fake-job"
        self.errors = None

    def result(self, timeout: int):
        return self.rows


class FakeBigQueryClient:
    def __init__(self, *, dry_run_bytes: int, rows: list[dict[str, object]] | None = None):
        self.dry_run_bytes = dry_run_bytes
        self.rows = rows or [{"category": "Jeans", "gross_sales": 123.45}]
        self.calls = []

    def query(self, sql: str, job_config):
        self.calls.append((sql, job_config))
        if job_config.dry_run:
            return FakeJob(total_bytes_processed=self.dry_run_bytes)
        return FakeJob(total_bytes_billed=self.dry_run_bytes, rows=self.rows)


def test_bigquery_runner_dry_runs_then_executes_with_limit(test_config, tmp_path):
    logger = EventLogger(tmp_path / "runs.jsonl")
    runner = BigQueryRunner(test_config, logger)
    fake_client = FakeBigQueryClient(dry_run_bytes=4096)
    runner._client = fake_client

    result = runner.execute(
        """
        SELECT p.category, SUM(oi.sale_price) AS gross_sales
        FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
        JOIN `bigquery-public-data.thelook_ecommerce.products` AS p
          ON oi.product_id = p.id
        GROUP BY p.category
        ORDER BY gross_sales DESC
        """,
        trace_id="trace-id",
    )

    assert len(fake_client.calls) == 2
    assert fake_client.calls[0][1].dry_run is True
    assert fake_client.calls[1][1].dry_run is None
    assert result.sql.endswith("LIMIT 25")
    assert result.rows == [{"category": "Jeans", "gross_sales": 123.45}]
    assert result.dry_run_bytes == 4096
    events = [
        json.loads(line)
        for line in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event"] == "sql_validation_succeeded" for event in events)
    assert any(event["event"] == "bigquery_query_succeeded" for event in events)


def test_bigquery_runner_blocks_over_budget_before_execution(test_config, tmp_path):
    logger = EventLogger(tmp_path / "runs.jsonl")
    runner = BigQueryRunner(test_config, logger)
    fake_client = FakeBigQueryClient(
        dry_run_bytes=test_config.bigquery.max_bytes_billed + 1
    )
    runner._client = fake_client

    with pytest.raises(QueryCostExceeded):
        runner.execute(
            "SELECT order_id FROM `bigquery-public-data.thelook_ecommerce.orders` LIMIT 5",
            trace_id="trace-id",
        )

    assert len(fake_client.calls) == 1
    assert fake_client.calls[0][1].dry_run is True
    events = [
        json.loads(line)
        for line in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    cost_events = [event for event in events if event["event"] == "bigquery_cost_exceeded"]
    assert cost_events[0]["dry_run_bytes"] == test_config.bigquery.max_bytes_billed + 1
