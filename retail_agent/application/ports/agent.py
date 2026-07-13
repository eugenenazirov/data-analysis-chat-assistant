from __future__ import annotations

from typing import Protocol

from retail_agent.domain.models import AnalysisReport, Conversation, UserQuestion


class AnalysisAgent(Protocol):
    async def analyze(
        self,
        question: UserQuestion,
        conversation: Conversation,
    ) -> AnalysisReport: ...
