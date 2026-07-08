from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    """Formatting preferences for an executive user."""

    user_id: str
    display_name: str
    preferred_format: str = Field(default="bullets", pattern="^(bullets|table|brief)$")
    tone: str = "clear, concise, executive-friendly"


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


class RetrievedTrio(BaseModel):
    id: str
    score: float
    question: str
    sql: str
    analyst_report: str
    tags: list[str] = Field(default_factory=list)


class QueryResult(BaseModel):
    sql: str
    rows: list[dict[str, Any]]
    total_rows: int
    dry_run_bytes: int | None = None
    total_bytes_billed: int | None = None
    job_id: str | None = None


class AnalysisReport(BaseModel):
    question: str
    answer: str
    highlights: list[str] = Field(default_factory=list)
    table: list[dict[str, Any]] = Field(default_factory=list)
    sql: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    followups: list[str] = Field(default_factory=list)
    refused: bool = False
    trace_id: str | None = None


class AgentFailure(BaseModel):
    question: str
    message: str
    trace_id: str | None = None
    retryable: bool = False
