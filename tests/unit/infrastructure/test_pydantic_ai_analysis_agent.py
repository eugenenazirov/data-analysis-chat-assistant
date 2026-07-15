from pydantic_ai.messages import ModelRequest, ModelResponse

from retail_agent.domain.models import (
    Conversation,
    ConversationRole,
    ToolResultSummary,
)
from retail_agent.infrastructure.agents.pydantic_ai_analysis_agent import (
    PydanticAIAnalysisAgent,
    _conversation_history,
    _conversation_query_results,
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
                    available_rows=1,
                    truncated=False,
                    row_limit=500,
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
    assert '"available_rows":1' in first_answer
    assert '"truncated":false' in first_answer
    assert '"row_limit":500' in first_answer


def test_conversation_history_converts_only_requested_trailing_groups():
    conversation = Conversation(max_retained_turns=6)
    for index in range(3):
        conversation = conversation.append(ConversationRole.user, f"question {index}")
        conversation = conversation.append(ConversationRole.assistant, f"answer {index}")

    history = _conversation_history(conversation, max_groups=1)

    assert len(history) == 1
    assert history[0][0].parts[0].content == "question 2"
    assert history[0][1].parts[0].content == "answer 2"


def test_conversation_query_results_restore_verified_rows_for_follow_ups():
    conversation = (
        Conversation()
        .append(ConversationRole.user, "Which region led?")
        .append(
            ConversationRole.assistant,
            "California led with 2 orders.",
            tool_result_summaries=(
                ToolResultSummary(
                    tool_name="run_sql_query",
                    summary="Verified query returned one row.",
                    sql="SELECT 'California' AS region, 2 AS completed_orders",
                    rows=({"region": "California", "completed_orders": 2},),
                    total_rows=1,
                    available_rows=1,
                    row_limit=500,
                ),
            ),
        )
        .append(ConversationRole.user, "Thanks")
        .append(ConversationRole.assistant, "You're welcome.")
    )

    results = _conversation_query_results(conversation)

    assert len(results) == 2
    assert results[0] is not None
    assert results[0].rows == [{"region": "California", "completed_orders": 2}]
    assert results[1] is None


def test_analysis_agent_builds_provider_model_once_and_reuses_it(test_config):
    calls = []
    model = object()
    adapter = PydanticAIAnalysisAgent(
        test_config,
        analytics=None,
        retrieval=None,
        chart_executor=None,
        telemetry=None,
        runner=None,
        model_factory=lambda: calls.append("built") or model,
    )

    assert adapter._get_analysis_model() is model
    assert adapter._get_analysis_model() is model
    assert adapter.analysis_model is model
    assert calls == ["built"]
