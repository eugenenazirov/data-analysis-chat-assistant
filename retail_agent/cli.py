"""Compatibility exports for the command-line presentation adapter."""

from retail_agent.presentation.cli.app import (
    BIGQUERY_SMOKE_SQL,
    EvalSuite,
    QualityEvalMode,
    app,
    ask,
    bq_smoke,
    chat,
    eval,
    index_golden,
)

__all__ = [
    "BIGQUERY_SMOKE_SQL",
    "EvalSuite",
    "QualityEvalMode",
    "app",
    "ask",
    "bq_smoke",
    "chat",
    "eval",
    "index_golden",
]
