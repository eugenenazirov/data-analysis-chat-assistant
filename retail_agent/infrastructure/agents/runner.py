from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from pydantic_ai import ModelSettings, UsageLimits
from pydantic_ai.messages import ModelMessage


class AnalysisAgentRunner(Protocol):
    """Minimal PydanticAI-compatible model runner boundary."""

    async def run(
        self,
        user_prompt: str,
        *,
        deps: Any,
        model: str,
        message_history: Sequence[ModelMessage] | None = None,
        conversation_id: str | None = None,
        model_settings: ModelSettings | None = None,
        usage_limits: UsageLimits | None = None,
    ) -> Any: ...
