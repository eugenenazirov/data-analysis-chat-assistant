from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

type FailureCode = Literal[
    "model_unavailable",
    "warehouse_unavailable",
    "warehouse_outcome_unknown",
    "retry_exhausted",
    "internal_error",
]


class AnalysisReport(BaseModel):
    question: str
    answer: str
    highlights: list[str] = Field(default_factory=list)
    table: list[dict[str, Any]] = Field(default_factory=list)
    sql: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)
    refused: bool = False
    degraded: bool = False
    trace_id: str | None = None


class AgentFailure(BaseModel):
    question: str
    message: str
    failure_code: FailureCode
    trace_id: str | None = None
    retryable: bool = False


type AnalysisResponse = AnalysisReport | AgentFailure
