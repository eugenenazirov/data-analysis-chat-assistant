"""Compatibility exports for the BigQuery analytics adapter."""

from retail_agent.domain.errors import (
    QueryCostExceeded,
    QueryExecutionError,
    QueryOutcomeUnknownError,
    QueryPreExecutionError,
)
from retail_agent.infrastructure.analytics.bigquery_adapter import (
    BigQueryAnalyticsAdapter,
    BigQueryRunner,
    _stable_job_id,
)

__all__ = [
    "BigQueryAnalyticsAdapter",
    "BigQueryRunner",
    "QueryCostExceeded",
    "QueryExecutionError",
    "QueryOutcomeUnknownError",
    "QueryPreExecutionError",
    "_stable_job_id",
]
