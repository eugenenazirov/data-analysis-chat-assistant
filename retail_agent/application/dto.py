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
    prompt_matches: bool

    @property
    def ready(self) -> bool:
        return self.revision_matches and self.prompt_matches


__all__ = [
    "AgentAnalysisResult",
    "AgentFailure",
    "AnalysisResponse",
    "AnalyzeQuestionResponse",
    "ChartSmokeCase",
    "ReviewerDiagnostics",
]
