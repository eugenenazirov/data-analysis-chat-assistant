from retail_agent.application.ports import ConversationRepository
from retail_agent.domain.models import Conversation


class StartConversation:
    def __init__(
        self,
        conversations: ConversationRepository,
        *,
        max_retained_turns: int,
    ) -> None:
        self.conversations = conversations
        self.max_retained_turns = max_retained_turns

    async def execute(self) -> str:
        conversation = Conversation(max_retained_turns=self.max_retained_turns)
        await self.conversations.save(conversation)
        return str(conversation.id)
