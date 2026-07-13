from retail_agent.application.ports import ConversationRepository
from retail_agent.domain.models import ConversationId


class ClearConversation:
    def __init__(self, conversations: ConversationRepository) -> None:
        self.conversations = conversations

    async def execute(self, conversation_id: str) -> bool:
        identifier = ConversationId(conversation_id)
        conversation = await self.conversations.get(identifier)
        if conversation is None:
            return False
        await self.conversations.save(conversation.clear())
        return True
