import pytest

from retail_agent.domain.models import Conversation, ConversationRole


def test_conversation_retains_only_complete_context_groups():
    conversation = Conversation(max_retained_turns=2)

    conversation = conversation.append(ConversationRole.user, "first")
    conversation = conversation.append(ConversationRole.assistant, "second")
    conversation = conversation.append(ConversationRole.user, "third")

    assert [turn.content for turn in conversation.turns] == ["third"]

    conversation = conversation.append(ConversationRole.assistant, "fourth")

    assert [turn.content for turn in conversation.turns] == ["third", "fourth"]
    assert conversation.completed_turn_count == 2


def test_conversation_rejects_out_of_order_roles():
    conversation = Conversation()

    with pytest.raises(ValueError, match="Expected the next conversation role to be user"):
        conversation.append(ConversationRole.assistant, "unexpected")


def test_clearing_conversation_preserves_session_identity():
    conversation = Conversation().append(ConversationRole.user, "question")

    cleared = conversation.clear()

    assert cleared.id == conversation.id
    assert cleared.turns == ()
    assert cleared.completed_turn_count == conversation.completed_turn_count
