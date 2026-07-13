from retail_agent.domain.models import Conversation, ConversationRole


def test_conversation_retains_only_configured_number_of_turns():
    conversation = Conversation(max_retained_turns=2)

    conversation = conversation.append(ConversationRole.user, "first")
    conversation = conversation.append(ConversationRole.assistant, "second")
    conversation = conversation.append(ConversationRole.user, "third")

    assert [turn.content for turn in conversation.turns] == ["second", "third"]


def test_clearing_conversation_preserves_session_identity():
    conversation = Conversation().append(ConversationRole.user, "question")

    cleared = conversation.clear()

    assert cleared.id == conversation.id
    assert cleared.turns == ()
