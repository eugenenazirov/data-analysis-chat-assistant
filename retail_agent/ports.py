from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from pydantic_ai.messages import ModelMessage

from retail_agent.models import QueryResult, RetrievedTrio


class WarehousePort(Protocol):
    """Read-only analytics warehouse boundary."""

    def describe_allowed_tables(self) -> str: ...

    def execute(self, sql: str, trace_id: str) -> QueryResult: ...


class KnowledgeRetrieverPort(Protocol):
    """Approved analyst-knowledge retrieval boundary."""

    def search(
        self, question: str, trace_id: str, limit: int = 3
    ) -> list[RetrievedTrio]: ...


class AnalysisAgentPort(Protocol):
    """Minimal PydanticAI-compatible model orchestration boundary."""

    async def run(
        self,
        user_prompt: str,
        *,
        deps: Any,
        model: str,
        message_history: Sequence[ModelMessage] | None = None,
        conversation_id: str | None = None,
    ) -> Any: ...
