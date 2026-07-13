from __future__ import annotations

import asyncio

from retail_agent.application.dto import AgentAnalysisResult
from retail_agent.application.use_cases import (
    AnalyzeQuestion,
    ClearConversation,
    StartConversation,
)
from retail_agent.domain.models import (
    AnalysisReport,
    ConversationId,
    ToolResultSummary,
    UserProfile,
)
from retail_agent.infrastructure.conversations.in_memory_repository import (
    InMemoryConversationRepository,
)


class RecordingAgent:
    def __init__(self) -> None:
        self.conversations = []

    async def analyze(self, question, conversation, user):
        self.conversations.append(conversation)
        return AgentAnalysisResult(
            response=AnalysisReport(
                question=question.root,
                answer="Contact analyst@example.com; revenue was 42.",
                table=[{"revenue": 42}],
                sql="SELECT 42 AS revenue",
            ),
            tool_results=(
                ToolResultSummary(
                    tool_name="run_sql_query",
                    summary="Contact analyst@example.com; returned one row.",
                    sql="SELECT 42 AS revenue",
                    rows=({"revenue": 42},),
                    total_rows=1,
                ),
            ),
        )


def _user() -> UserProfile:
    return UserProfile(user_id="manager", display_name="Manager")


def test_analyze_question_redacts_and_persists_bounded_conversation():
    repository = InMemoryConversationRepository()
    agent = RecordingAgent()
    use_case = AnalyzeQuestion(agent, repository, max_retained_turns=4)

    first = asyncio.run(use_case.execute("Revenue?", user=_user()))
    second = asyncio.run(
        use_case.execute(
            "Compare it.",
            user=_user(),
            conversation_id=first.conversation_id,
        )
    )
    conversation = asyncio.run(repository.get(ConversationId(second.conversation_id)))

    assert first.response.answer.startswith("Contact [REDACTED_EMAIL]")
    assert conversation is not None
    assert [turn.content for turn in conversation.turns] == [
        "Revenue?",
        "Contact [REDACTED_EMAIL]; revenue was 42.",
        "Compare it.",
        "Contact [REDACTED_EMAIL]; revenue was 42.",
    ]
    tool_result = conversation.turns[-1].tool_result_summaries[0]
    assert "[REDACTED_EMAIL]" in tool_result.summary
    assert agent.conversations[1].turns[-1].content.startswith("Contact [REDACTED_EMAIL]")


def test_start_and_clear_conversation_preserve_session():
    repository = InMemoryConversationRepository()
    start = StartConversation(repository, max_retained_turns=4)
    clear = ClearConversation(repository)

    conversation_id = asyncio.run(start.execute())
    assert asyncio.run(clear.execute(conversation_id)) is True

    conversation = asyncio.run(repository.get(ConversationId(conversation_id)))
    assert conversation is not None
    assert conversation.turns == ()
    assert asyncio.run(clear.execute("missing")) is False
