from __future__ import annotations

from typing import Protocol

from retail_agent.domain.models import ChartArtifact, ChartRequest


class ChartCodeExecutor(Protocol):
    async def execute(self, request: ChartRequest) -> ChartArtifact: ...
