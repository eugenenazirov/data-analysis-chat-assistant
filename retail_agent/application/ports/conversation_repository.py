from __future__ import annotations

from typing import Protocol

from retail_agent.domain.models import Conversation, ConversationId


class ConversationRepository(Protocol):
    async def get(self, conversation_id: ConversationId) -> Conversation | None: ...

    async def save(self, conversation: Conversation) -> None: ...

    async def clear(self, conversation_id: ConversationId) -> None: ...
