from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from retail_agent.domain.models.chart import ChartArtifact

NARRATIVE_OUTPUT_RULE = (
    "Keep narrative fields concise and do not reproduce verified query rows as a "
    "Markdown table or row-by-row dump; the runtime attaches the verified table."
)

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
    chart_artifact: ChartArtifact | None = None
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


class DataAnalysisResult(BaseModel):
    kind: Literal["data_analysis"] = "data_analysis"
    direct_answer: str
    highlights: list[str] = Field(
        default_factory=list,
        description=f"Concise evidence-backed findings. {NARRATIVE_OUTPUT_RULE}",
    )
    supporting_evidence: list[str] = Field(
        default_factory=list,
        description=f"Short prose evidence only. {NARRATIVE_OUTPUT_RULE}",
    )
    caveats: list[str] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)
    chart_artifact: ChartArtifact | None = None


class SchemaExplanationResult(BaseModel):
    kind: Literal["schema_explanation"] = "schema_explanation"
    explanation: str
    caveats: list[str] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)


class ClarificationRequest(BaseModel):
    kind: Literal["clarification"] = "clarification"
    question: str


class UnsupportedRequest(BaseModel):
    kind: Literal["unsupported"] = "unsupported"
    reason: str


class ExecutionFailure(BaseModel):
    kind: Literal["execution_failure"] = "execution_failure"
    message: str
    retryable: bool = False


type AnalysisResult = Annotated[
    DataAnalysisResult
    | SchemaExplanationResult
    | ClarificationRequest
    | UnsupportedRequest
    | ExecutionFailure,
    Field(discriminator="kind"),
]
