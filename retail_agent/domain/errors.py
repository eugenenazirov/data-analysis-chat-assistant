from typing import Literal

type ChartExecutionFailureCode = Literal[
    "captured_output_limit",
    "data_shape_error",
    "invalid_output",
    "missing_dependency",
    "output_missing",
    "output_too_large",
    "process_failed",
    "runtime_error",
    "source_too_large",
    "syntax_error",
    "timeout",
    "unsafe_source",
]


class RetailAgentError(RuntimeError):
    """Base exception that may cross application boundaries."""


class AnalyticsError(RetailAgentError):
    """Analytics execution failed without exposing an SDK-specific exception."""


class QueryExecutionError(AnalyticsError):
    pass


class QueryPreExecutionError(QueryExecutionError):
    """Failure known to occur before a paid query job is submitted."""


class QueryOutcomeUnknownError(QueryExecutionError):
    """Failure after submission where re-execution could duplicate query cost."""

    def __init__(self, message: str, *, job_id: str):
        super().__init__(message)
        self.job_id = job_id


class QueryCostExceeded(QueryPreExecutionError):
    pass


class RetrievalError(RetailAgentError):
    """Golden-example retrieval failed."""


class ChartExecutionError(RetailAgentError):
    """Chart code could not produce a valid artifact."""

    def __init__(
        self,
        message: str,
        *,
        code: ChartExecutionFailureCode,
        repair_hint: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.repair_hint = repair_hint
