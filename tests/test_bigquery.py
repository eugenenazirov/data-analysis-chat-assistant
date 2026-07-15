from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from google.api_core import exceptions
from google.cloud import bigquery

from retail_agent.bigquery import (
    BigQueryRunner,
    QueryCostExceeded,
    QueryOutcomeUnknownError,
    QueryPreExecutionError,
)
from retail_agent.observability import EventLogger


class FakeRowIterator(list):
    def __init__(self, rows, *, total_rows: int):
        super().__init__(rows)
        self.total_rows = total_rows


class FakeJob:
    def __init__(
        self,
        *,
        total_bytes_processed: int = 0,
        total_bytes_billed: int = 0,
        rows: list[dict[str, object]] | None = None,
        available_rows: int | None = None,
    ):
        self.total_bytes_processed = total_bytes_processed
        self.total_bytes_billed = total_bytes_billed
        self.rows = rows or []
        self.available_rows = available_rows or len(self.rows)
        self.max_results = None
        self.job_id = "fake-job"
        self.errors = None
        self.cancelled = False

    def result(self, timeout: int, max_results: int | None = None):
        self.max_results = max_results
        return FakeRowIterator(
            self.rows[:max_results],
            total_rows=self.available_rows,
        )

    def cancel(self):
        self.cancelled = True


class FakeBigQueryClient:
    def __init__(
        self,
        *,
        dry_run_bytes: int,
        rows: list[dict[str, object]] | None = None,
        available_rows: int | None = None,
    ):
        self.dry_run_bytes = dry_run_bytes
        self.rows = rows or [{"category": "Jeans", "gross_sales": 123.45}]
        self.available_rows = available_rows or len(self.rows)
        self.calls = []
        self.execution_job = None

    def query(self, sql: str, job_config, **kwargs):
        self.calls.append((sql, job_config, kwargs))
        if job_config.dry_run:
            return FakeJob(total_bytes_processed=self.dry_run_bytes)
        self.execution_job = FakeJob(
            total_bytes_billed=self.dry_run_bytes,
            rows=self.rows,
            available_rows=self.available_rows,
        )
        return self.execution_job


def test_bigquery_runner_dry_runs_then_executes_with_bounded_client_fetch(
    test_config, tmp_path
):
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
    assert "LIMIT" not in result.sql.upper()
    assert result.rows == [{"category": "Jeans", "gross_sales": 123.45}]
    assert result.available_rows == 1
    assert result.truncated is False
    assert result.row_limit == 25
    assert fake_client.execution_job.max_results == 25
    assert result.dry_run_bytes == 4096
    assert result.job_id == fake_client.calls[1][2]["job_id"]
    assert fake_client.calls[1][2]["job_retry"] is None
    assert result.job_id.startswith("retail_agent_trace_id_")
    events = [
        json.loads(line)
        for line in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["event"] == "sql_validation_succeeded" for event in events)
    assert any(event["event"] == "bigquery_query_succeeded" for event in events)


def test_bigquery_runner_marks_results_above_client_cap_as_truncated(
    test_config, tmp_path
):
    rows = [{"order_id": index} for index in range(30)]
    fake_client = FakeBigQueryClient(
        dry_run_bytes=4096,
        rows=rows,
        available_rows=30,
    )
    runner = BigQueryRunner(test_config, EventLogger(tmp_path / "runs.jsonl"))
    runner._client = fake_client

    result = runner.execute(
        "SELECT order_id FROM `bigquery-public-data.thelook_ecommerce.orders`",
        trace_id="trace-id",
    )

    assert result.total_rows == 25
    assert result.available_rows == 30
    assert result.truncated is True
    assert result.row_limit == 25
    assert len(result.rows) == 25


def test_six_month_category_result_returns_all_156_cells(test_config, tmp_path):
    configured = test_config.model_copy(
        update={
            "bigquery": test_config.bigquery.model_copy(
                update={"max_result_rows": 500}
            )
        }
    )
    rows = [
        {
            "month": f"2026-{month:02d}-01",
            "category": f"Category {category:02d}",
            "revenue": month * category * 100,
        }
        for category in range(1, 27)
        for month in range(1, 7)
    ]
    fake_client = FakeBigQueryClient(
        dry_run_bytes=4096,
        rows=rows,
        available_rows=156,
    )
    runner = BigQueryRunner(configured, EventLogger(tmp_path / "runs.jsonl"))
    runner._client = fake_client

    result = runner.execute(
        """
        SELECT DATE_TRUNC(DATE(oi.created_at), MONTH) AS month,
               p.category,
               SUM(oi.sale_price) AS revenue
        FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
        JOIN `bigquery-public-data.thelook_ecommerce.products` AS p
          ON oi.product_id = p.id
        GROUP BY month, p.category
        ORDER BY month, p.category
        """,
        trace_id="trace-id",
    )

    assert result.total_rows == 156
    assert result.available_rows == 156
    assert result.truncated is False
    assert result.row_limit == 500
    assert "LIMIT" not in result.sql.upper()


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


def test_bigquery_runner_lazily_builds_configured_client(test_config, tmp_path, monkeypatch):
    calls = []

    def fake_client(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(bigquery, "Client", fake_client)
    runner = BigQueryRunner(test_config, EventLogger(tmp_path / "runs.jsonl"))

    first = runner.client
    second = runner.client

    assert first is second
    assert calls == [{"location": "US"}]


def test_describe_allowed_tables_tolerates_per_table_schema_failure(test_config, tmp_path):
    class SchemaClient:
        def __init__(self):
            self.calls = 0

        def get_table(self, full_name):
            self.calls += 1
            if full_name.endswith("users"):
                raise RuntimeError("metadata unavailable")
            return SimpleNamespace(
                schema=[SimpleNamespace(name="order_id", field_type="INTEGER")]
            )

    runner = BigQueryRunner(test_config, EventLogger(tmp_path / "runs.jsonl"))
    client = SchemaClient()
    runner._client = client

    description = runner.describe_allowed_tables()
    cached_description = runner.describe_allowed_tables()

    assert "order_id INTEGER" in description
    assert "users`: schema unavailable" in description
    assert cached_description == description
    assert client.calls == len(test_config.bigquery.allowed_tables)


def test_describe_allowed_tables_exposes_only_configured_safe_columns(
    test_config, tmp_path
):
    class SchemaClient:
        def get_table(self, full_name):
            return SimpleNamespace(
                schema=[
                    SimpleNamespace(name="order_id", field_type="INTEGER"),
                    SimpleNamespace(name="postal_code", field_type="STRING"),
                    SimpleNamespace(name="email", field_type="STRING"),
                ]
            )

    runner = BigQueryRunner(test_config, EventLogger(tmp_path / "runs.jsonl"))
    runner._client = SchemaClient()

    description = runner.describe_allowed_tables()

    assert "order_id INTEGER" in description
    assert "postal_code" not in description
    assert "email" not in description


def test_bigquery_runner_logs_validation_failure(test_config, tmp_path):
    log_path = tmp_path / "runs.jsonl"
    runner = BigQueryRunner(test_config, EventLogger(log_path))

    with pytest.raises(ValueError):
        runner.execute(
            "DELETE FROM `bigquery-public-data.thelook_ecommerce.orders` WHERE 1=1",
            "trace",
        )

    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert events[-1]["event"] == "sql_validation_failed"


def test_bigquery_runner_wraps_dry_run_api_failure(test_config, tmp_path):
    class DryRunFailureClient:
        def query(self, sql, job_config, **kwargs):
            raise exceptions.ServiceUnavailable("BigQuery unavailable")

    runner = BigQueryRunner(test_config, EventLogger(tmp_path / "runs.jsonl"))
    runner._client = DryRunFailureClient()

    with pytest.raises(QueryPreExecutionError, match="dry run failed"):
        runner.execute(
            "SELECT order_id FROM `bigquery-public-data.thelook_ecommerce.orders` LIMIT 5",
            "trace",
        )


def test_bigquery_runner_wraps_execution_api_failure(test_config, tmp_path):
    class ExecutionFailureClient:
        def __init__(self):
            self.calls = 0

        def query(self, sql, job_config, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return FakeJob(total_bytes_processed=10)
            raise exceptions.ServiceUnavailable("BigQuery unavailable")

    runner = BigQueryRunner(test_config, EventLogger(tmp_path / "runs.jsonl"))
    runner._client = ExecutionFailureClient()

    with pytest.raises(QueryOutcomeUnknownError, match="query outcome is unknown") as exc_info:
        runner.execute(
            "SELECT order_id FROM `bigquery-public-data.thelook_ecommerce.orders` LIMIT 5",
            "trace",
        )

    assert exc_info.value.job_id.startswith("retail_agent_trace_")


def test_bigquery_runner_cancels_timed_out_job(test_config, tmp_path):
    timeout_job = FakeJob(total_bytes_billed=10)

    def timeout_result(timeout, max_results=None):
        raise TimeoutError("slow")

    timeout_job.result = timeout_result

    class TimeoutClient:
        def __init__(self):
            self.calls = 0

        def query(self, sql, job_config, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return FakeJob(total_bytes_processed=10)
            return timeout_job

    runner = BigQueryRunner(test_config, EventLogger(tmp_path / "runs.jsonl"))
    runner._client = TimeoutClient()

    with pytest.raises(QueryOutcomeUnknownError, match="outcome is unknown") as exc_info:
        runner.execute(
            "SELECT order_id FROM `bigquery-public-data.thelook_ecommerce.orders` LIMIT 5",
            "trace",
        )

    assert timeout_job.cancelled is True
    assert exc_info.value.job_id.startswith("retail_agent_trace_")
