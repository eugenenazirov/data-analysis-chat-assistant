from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator


class ConversationId(RootModel[str]):
    @classmethod
    def new(cls) -> Self:
        return cls(uuid.uuid4().hex)

    @field_validator("root")
    @classmethod
    def validate_value(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Conversation ID must not be empty.")
        return value

    def __str__(self) -> str:
        return self.root


class UserQuestion(RootModel[str]):
    @field_validator("root")
    @classmethod
    def validate_value(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Question must not be empty.")
        return value

    def __str__(self) -> str:
        return self.root


class ContextualizedQuestion(UserQuestion):
    """A standalone question with conversation references resolved."""


class ConversationRole(StrEnum):
    user = "user"
    assistant = "assistant"


class ToolResultSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_name: str
    summary: str
    sql: str | None = None
    rows: tuple[dict[str, Any], ...] = Field(default=(), max_length=20)
    total_rows: int | None = Field(default=None, ge=0)
    available_rows: int | None = Field(default=None, ge=0)
    truncated: bool = False
    row_limit: int | None = Field(default=None, ge=1)
    artifact_path: str | None = None


class ConversationTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: ConversationRole
    content: str
    tool_result_summaries: tuple[ToolResultSummary, ...] = ()
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Conversation(BaseModel):
    """Conversation aggregate with ordered, session-local, bounded turns."""

    model_config = ConfigDict(frozen=True)

    id: ConversationId = Field(default_factory=ConversationId.new)
    turns: tuple[ConversationTurn, ...] = ()
    max_retained_turns: int = Field(default=20, ge=2, le=200)
    completed_turn_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def append(
        self,
        role: ConversationRole,
        content: str,
        *,
        tool_result_summaries: tuple[ToolResultSummary, ...] = (),
    ) -> Self:
        expected_role = (
            ConversationRole.assistant
            if self.turns and self.turns[-1].role is ConversationRole.user
            else ConversationRole.user
        )
        if role is not expected_role:
            raise ValueError(f"Expected the next conversation role to be {expected_role}.")
        turn = ConversationTurn(
            role=role,
            content=content,
            tool_result_summaries=tool_result_summaries,
        )
        bounded = (*self.turns, turn)[-self.max_retained_turns :]
        if bounded and bounded[0].role is ConversationRole.assistant:
            bounded = bounded[1:]
        completed_turn_count = self.completed_turn_count + (
            1 if role is ConversationRole.assistant else 0
        )
        return self.model_copy(
            update={
                "turns": bounded,
                "completed_turn_count": completed_turn_count,
                "updated_at": turn.occurred_at,
            }
        )

    def clear(self) -> Self:
        return self.model_copy(
            update={"turns": (), "updated_at": datetime.now(UTC)}
        )


class UserPreferences(BaseModel):
    preferred_format: str = Field(default="bullets", pattern="^(bullets|table|brief)$")
    tone: str = "clear, concise, executive-friendly"


class UserProfile(UserPreferences):
    """Formatting preferences for an executive user."""

    user_id: str
    display_name: str
