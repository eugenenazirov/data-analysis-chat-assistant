from __future__ import annotations

from pydantic import BaseModel, Field


class OperationalMetrics(BaseModel):
    trace_ids: list[str] = Field(default_factory=list)
    duration_ms: int = Field(default=0, ge=0)
    turn_durations_ms: list[int] = Field(default_factory=list)
    provider_requests: int | None = Field(default=None, ge=0)
    retrieval_requests: int = Field(default=0, ge=0)
    query_attempts: int = Field(default=0, ge=0)
    sql_retries: int = Field(default=0, ge=0)
    sql_retry_reasons: list[str] = Field(default_factory=list)
    output_retries: int = Field(default=0, ge=0)
    output_retry_reasons: list[str] = Field(default_factory=list)
    bigquery_dry_runs: int = Field(default=0, ge=0)
    bigquery_executions: int = Field(default=0, ge=0)
    bigquery_job_ids: list[str] = Field(default_factory=list)
    duplicate_warehouse_executions: int = Field(default=0, ge=0)
    tool_sequence: list[str] = Field(default_factory=list)
    tool_order_compliant: bool = True
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    dry_run_bytes: int = Field(default=0, ge=0)
    billed_bytes: int = Field(default=0, ge=0)
    cache_hit: bool | None = None
    chart_duration_ms: int | None = Field(default=None, ge=0)
    chart_artifact_bytes: int | None = Field(default=None, ge=0)


def merge_operational_metrics(
    metrics: list[OperationalMetrics],
) -> OperationalMetrics:
    if not metrics:
        return OperationalMetrics()

    def optional_sum(field: str) -> int | None:
        values = [getattr(item, field) for item in metrics]
        return sum(values) if all(value is not None for value in values) else None

    chart_durations = [
        item.chart_duration_ms for item in metrics if item.chart_duration_ms is not None
    ]
    chart_sizes = [
        item.chart_artifact_bytes for item in metrics if item.chart_artifact_bytes is not None
    ]
    cache_values = [item.cache_hit for item in metrics if item.cache_hit is not None]
    return OperationalMetrics(
        trace_ids=[trace_id for item in metrics for trace_id in item.trace_ids],
        duration_ms=sum(item.duration_ms for item in metrics),
        turn_durations_ms=[
            duration
            for item in metrics
            for duration in (item.turn_durations_ms or [item.duration_ms])
        ],
        provider_requests=optional_sum("provider_requests"),
        retrieval_requests=sum(item.retrieval_requests for item in metrics),
        query_attempts=sum(item.query_attempts for item in metrics),
        sql_retries=sum(item.sql_retries for item in metrics),
        sql_retry_reasons=[reason for item in metrics for reason in item.sql_retry_reasons],
        output_retries=sum(item.output_retries for item in metrics),
        output_retry_reasons=[reason for item in metrics for reason in item.output_retry_reasons],
        bigquery_dry_runs=sum(item.bigquery_dry_runs for item in metrics),
        bigquery_executions=sum(item.bigquery_executions for item in metrics),
        bigquery_job_ids=[job_id for item in metrics for job_id in item.bigquery_job_ids],
        duplicate_warehouse_executions=sum(item.duplicate_warehouse_executions for item in metrics),
        tool_sequence=[tool for item in metrics for tool in item.tool_sequence],
        tool_order_compliant=all(item.tool_order_compliant for item in metrics),
        input_tokens=optional_sum("input_tokens"),
        cached_input_tokens=optional_sum("cached_input_tokens"),
        reasoning_tokens=optional_sum("reasoning_tokens"),
        output_tokens=optional_sum("output_tokens"),
        total_tokens=optional_sum("total_tokens"),
        dry_run_bytes=sum(item.dry_run_bytes for item in metrics),
        billed_bytes=sum(item.billed_bytes for item in metrics),
        cache_hit=any(cache_values) if cache_values else None,
        chart_duration_ms=sum(chart_durations) if chart_durations else None,
        chart_artifact_bytes=max(chart_sizes) if chart_sizes else None,
    )
