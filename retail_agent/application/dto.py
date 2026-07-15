from __future__ import annotations

from pydantic import BaseModel

from retail_agent.domain.models import (
    AgentFailure,
    AnalysisResponse,
    ChartArtifact,
    ToolResultSummary,
)


class AgentAnalysisResult(BaseModel):
    response: AnalysisResponse
    tool_results: tuple[ToolResultSummary, ...] = ()


class AnalyzeQuestionResponse(BaseModel):
    response: AnalysisResponse
    conversation_id: str

    @property
    def failed(self) -> bool:
        return isinstance(self.response, AgentFailure)


class ChartSmokeCase(BaseModel):
    case: str
    artifact: ChartArtifact


class ReviewerDiagnostics(BaseModel):
    values: tuple[tuple[str, str], ...]
    revision_matches: bool


__all__ = [
    "AgentAnalysisResult",
    "AgentFailure",
    "AnalysisResponse",
    "AnalyzeQuestionResponse",
    "ChartSmokeCase",
    "ReviewerDiagnostics",
]
