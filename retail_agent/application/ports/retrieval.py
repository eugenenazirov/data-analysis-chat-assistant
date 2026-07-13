from __future__ import annotations

from typing import Protocol

from retail_agent.domain.models import ContextualizedQuestion, GoldenExample


class GoldenExampleRepository(Protocol):
    async def search(
        self,
        question: ContextualizedQuestion,
        limit: int,
    ) -> list[GoldenExample]: ...
