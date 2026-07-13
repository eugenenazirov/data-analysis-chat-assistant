from pydantic_ai.messages import ModelRequest, ModelResponse

from retail_agent.domain.models import (
    Conversation,
    ConversationRole,
    ToolResultSummary,
)
from retail_agent.infrastructure.agents.pydantic_ai_analysis_agent import (
    _conversation_history,
)


def test_conversation_history_preserves_all_recent_turns_and_tool_evidence():
    conversation = (
        Conversation()
        .append(ConversationRole.user, "How many orders were placed?")
        .append(
            ConversationRole.assistant,
            "There were 42 orders.",
            tool_result_summaries=(
                ToolResultSummary(
                    tool_name="run_sql_query",
                    summary="Verified query returned one row.",
                    sql="SELECT 42 AS order_count",
                    rows=({"order_count": 42},),
                    total_rows=1,
                ),
            ),
        )
        .append(ConversationRole.user, "Which categories led?")
        .append(ConversationRole.assistant, "Outerwear led.")
    )

    history = _conversation_history(conversation)

    assert len(history) == 2
    assert all(isinstance(turn[0], ModelRequest) for turn in history)
    assert all(isinstance(turn[1], ModelResponse) for turn in history)
    first_answer = history[0][1].parts[0].content
    assert "There were 42 orders." in first_answer
    assert "SELECT 42 AS order_count" in first_answer
    assert '"order_count":42' in first_answer
