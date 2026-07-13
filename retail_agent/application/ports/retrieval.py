from __future__ import annotations

from typing import Protocol

from retail_agent.domain.models import GoldenExample


class GoldenExampleRepository(Protocol):
    def search(
        self,
        question: str,
        trace_id: str,
        limit: int = 3,
    ) -> list[GoldenExample]: ...
