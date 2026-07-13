from __future__ import annotations

from pydantic import BaseModel

from retail_agent.domain.models import AnalysisResponse, ConversationId


class AnalyzeQuestionResponse(BaseModel):
    response: AnalysisResponse
    conversation_id: ConversationId
