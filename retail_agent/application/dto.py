from __future__ import annotations

from pydantic import BaseModel

from retail_agent.domain.models import (
    AgentFailure,
    AnalysisResponse,
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


__all__ = [
    "AgentAnalysisResult",
    "AgentFailure",
    "AnalysisResponse",
    "AnalyzeQuestionResponse",
]
