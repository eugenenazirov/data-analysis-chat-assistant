from __future__ import annotations

from typing import Protocol

from retail_agent.domain.models import QueryResult, SafeSql


class AnalyticsGateway(Protocol):
    async def describe_allowed_tables(self) -> str: ...

    async def execute(self, query: SafeSql) -> QueryResult: ...
