class RetailAgentError(Exception):
    """Base exception that may cross application boundaries."""


class AnalyticsError(RetailAgentError):
    """Analytics execution failed without exposing an SDK-specific exception."""


class RetrievalError(RetailAgentError):
    """Golden-example retrieval failed."""


class ChartExecutionError(RetailAgentError):
    """Chart code could not produce a valid artifact."""
