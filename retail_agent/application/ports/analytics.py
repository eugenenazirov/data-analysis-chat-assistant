from __future__ import annotations

from typing import Protocol

from retail_agent.domain.models import QueryResult


class AnalyticsGateway(Protocol):
    def describe_allowed_tables(self) -> str: ...

    def execute(self, sql: str, trace_id: str) -> QueryResult: ...
