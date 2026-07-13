from __future__ import annotations

from typing import Protocol

from retail_agent.application.dto import AgentAnalysisResult
from retail_agent.domain.models import Conversation, UserProfile, UserQuestion


class AnalysisAgent(Protocol):
    async def analyze(
        self,
        question: UserQuestion,
        conversation: Conversation,
        user: UserProfile,
    ) -> AgentAnalysisResult: ...
