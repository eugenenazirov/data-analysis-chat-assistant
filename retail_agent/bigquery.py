from __future__ import annotations

import time
from typing import Any

from retail_agent.config import AgentConfig
from retail_agent.models import QueryResult
from retail_agent.observability import EventLogger
from retail_agent.sql_guard import validate_and_prepare_sql


class QueryExecutionError(RuntimeError):
    pass


class QueryCostExceeded(QueryExecutionError):
    pass


class BigQueryRunner:
    def __init__(self, config: AgentConfig, logger: EventLogger):
        self.config = config
        self.logger = logger
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                from google.cloud import bigquery
            except ImportError as exc:
                raise QueryExecutionError(
                    "google-cloud-bigquery is not installed."
                ) from exc
            kwargs: dict[str, Any] = {}
            if self.config.bigquery.project:
                kwargs["project"] = self.config.bigquery.project
            if self.config.bigquery.location:
                kwargs["location"] = self.config.bigquery.location
            self._client = bigquery.Client(**kwargs)
        return self._client

    def describe_allowed_tables(self) -> str:
        lines: list[str] = []
        for table_name in self.config.bigquery.allowed_tables:
            full_name = f"{self.config.bigquery.dataset}.{table_name}"
            try:
                table = self.client.get_table(full_name)
                columns = ", ".join(
                    f"{field.name} {field.field_type}" for field in table.schema
                )
                lines.append(f"- `{full_name}`: {columns}")
            except Exception:
                lines.append(f"- `{full_name}`: schema unavailable at startup")
        return "\n".join(lines)

    def execute(self, sql: str, trace_id: str) -> QueryResult:
        from google.api_core import exceptions
        from google.cloud import bigquery

        start = time.perf_counter()
        validation = validate_and_prepare_sql(sql, self.config)
        labels = {"app": self.config.bigquery.job_label_app, "trace": trace_id[:32]}

        dry_config = bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,
            maximum_bytes_billed=self.config.bigquery.max_bytes_billed,
            labels=labels,
        )
        try:
            dry_job = self.client.query(validation.safe_sql, job_config=dry_config)
            dry_bytes = int(dry_job.total_bytes_processed or 0)
        except exceptions.GoogleAPICallError as exc:
            self.logger.event(trace_id, "bigquery_dry_run_failed", error=str(exc))
            raise QueryExecutionError(f"BigQuery dry run failed: {exc}") from exc

        if dry_bytes > self.config.bigquery.max_bytes_billed:
            raise QueryCostExceeded(
                "Query would process "
                f"{dry_bytes} bytes, above cap {self.config.bigquery.max_bytes_billed}."
            )

        run_config = bigquery.QueryJobConfig(
            maximum_bytes_billed=self.config.bigquery.max_bytes_billed,
            labels=labels,
            job_timeout_ms=self.config.bigquery.timeout_seconds * 1000,
        )
        try:
            job = self.client.query(validation.safe_sql, job_config=run_config)
            rows_iter = job.result(timeout=self.config.bigquery.timeout_seconds)
            rows = [dict(row.items()) for row in rows_iter]
        except exceptions.GoogleAPICallError as exc:
            errors = getattr(locals().get("job", None), "errors", None)
            self.logger.event(
                trace_id, "bigquery_query_failed", error=str(exc), details=errors
            )
            raise QueryExecutionError(f"BigQuery query failed: {exc}") from exc
        except TimeoutError as exc:
            if "job" in locals():
                job.cancel()
            raise QueryExecutionError("BigQuery query timed out.") from exc

        duration_ms = int((time.perf_counter() - start) * 1000)
        self.logger.event(
            trace_id,
            "bigquery_query_succeeded",
            tables=validation.tables,
            rows=len(rows),
            dry_run_bytes=dry_bytes,
            total_bytes_billed=getattr(job, "total_bytes_billed", None),
            duration_ms=duration_ms,
        )
        return QueryResult(
            sql=validation.safe_sql,
            rows=rows,
            total_rows=len(rows),
            dry_run_bytes=dry_bytes,
            total_bytes_billed=getattr(job, "total_bytes_billed", None),
            job_id=getattr(job, "job_id", None),
        )
