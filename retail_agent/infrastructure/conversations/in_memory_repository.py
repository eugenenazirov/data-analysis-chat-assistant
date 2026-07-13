from __future__ import annotations

import asyncio

from retail_agent.domain.models import Conversation, ConversationId


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}
        self._lock = asyncio.Lock()

    async def get(self, conversation_id: ConversationId) -> Conversation | None:
        async with self._lock:
            conversation = self._conversations.get(str(conversation_id))
            return conversation.model_copy(deep=True) if conversation else None

    async def save(self, conversation: Conversation) -> None:
        async with self._lock:
            self._conversations[str(conversation.id)] = conversation.model_copy(deep=True)

    async def clear(self, conversation_id: ConversationId) -> None:
        async with self._lock:
            self._conversations.pop(str(conversation_id), None)
