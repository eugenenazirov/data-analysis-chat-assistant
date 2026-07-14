from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, RootModel, field_validator


class SafeSql(RootModel[str]):
    @field_validator("root")
    @classmethod
    def validate_value(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("SQL must not be empty.")
        return value

    def __str__(self) -> str:
        return self.root


class GoldenTrio(BaseModel):
    id: str
    question: str
    sql: str
    analyst_report: str
    tags: list[str] = Field(default_factory=list)
    created_at: datetime | None = None

    def embedding_text(self) -> str:
        tags = ", ".join(self.tags)
        return (
            f"Question: {self.question}\n"
            f"SQL: {self.sql}\n"
            f"Analyst report: {self.analyst_report}\n"
            f"Tags: {tags}"
        )


class GoldenExample(BaseModel):
    id: str
    score: float
    question: str
    sql: str
    analyst_report: str
    tags: list[str] = Field(default_factory=list)


RetrievedTrio = GoldenExample


class QueryResult(BaseModel):
    sql: str
    rows: list[dict[str, Any]]
    total_rows: int
    dry_run_bytes: int | None = None
    total_bytes_billed: int | None = None
    job_id: str | None = None
    cache_hit: bool | None = None
