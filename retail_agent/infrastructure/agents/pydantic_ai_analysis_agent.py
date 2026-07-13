from __future__ import annotations

import json

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

from retail_agent.agent import ConversationState, build_analysis_agent, run_question
from retail_agent.application.dto import AgentAnalysisResult
from retail_agent.domain.models import (
    Conversation,
    ConversationRole,
    ToolResultSummary,
    UserProfile,
    UserQuestion,
)
from retail_agent.infrastructure.analytics.bigquery_adapter import (
    BigQueryAnalyticsAdapter,
)
from retail_agent.infrastructure.observability import EventLogger
from retail_agent.infrastructure.retrieval.qdrant_adapter import (
    QdrantGoldenExampleRepository,
)
from retail_agent.infrastructure.settings import ApplicationSettings
from retail_agent.ports import AnalysisAgentPort


class PydanticAIAnalysisAgent:
    def __init__(
        self,
        config: ApplicationSettings,
        analytics: BigQueryAnalyticsAdapter,
        retrieval: QdrantGoldenExampleRepository,
        telemetry: EventLogger,
        runner: AnalysisAgentPort | None = None,
    ) -> None:
        self.config = config
        self.analytics = analytics
        self.retrieval = retrieval
        self.telemetry = telemetry
        self.runner = runner or build_analysis_agent(config)

    async def analyze(
        self,
        question: UserQuestion,
        conversation: Conversation,
        user: UserProfile,
    ) -> AgentAnalysisResult:
        previous_questions = tuple(
            turn.content for turn in conversation.turns if turn.role is ConversationRole.user
        )
        legacy_state = ConversationState(
            session_id=str(conversation.id),
            completed_turns=_conversation_history(conversation)[
                -self.config.conversation.max_history_turns :
            ],
            recent_questions=previous_questions[-self.config.conversation.max_history_turns :],
            turn_index=len(previous_questions),
        )
        turn = await run_question(
            question.root,
            config=self.config,
            bigquery=self.analytics,
            golden_store=self.retrieval,
            logger=self.telemetry,
            user_id=user.user_id,
            conversation=legacy_state,
            analysis_agent=self.runner,
        )
        tool_results: tuple[ToolResultSummary, ...] = ()
        if turn.query_result is not None:
            query_result = turn.query_result
            tool_results = (
                ToolResultSummary(
                    tool_name="run_sql_query",
                    summary=f"Verified query returned {query_result.total_rows} rows.",
                    sql=query_result.sql,
                    rows=tuple(query_result.rows[:20]),
                    total_rows=query_result.total_rows,
                ),
            )
        return AgentAnalysisResult(
            response=turn.response,
            tool_results=tool_results,
        )


def _conversation_history(
    conversation: Conversation,
) -> tuple[tuple[ModelMessage, ...], ...]:
    """Translate persisted domain turns into complete PydanticAI history groups."""

    completed: list[tuple[ModelMessage, ...]] = []
    pending: list[ModelMessage] = []
    for turn in conversation.turns:
        if turn.role is ConversationRole.user:
            if pending:
                completed.append(tuple(pending))
            pending = [ModelRequest(parts=[UserPromptPart(content=turn.content)])]
            continue

        assistant_content = _assistant_history_content(
            turn.content,
            turn.tool_result_summaries,
        )
        pending.append(ModelResponse(parts=[TextPart(content=assistant_content)]))
        completed.append(tuple(pending))
        pending = []

    if pending:
        completed.append(tuple(pending))
    return tuple(completed)


def _assistant_history_content(
    response_text: str,
    tool_results: tuple[ToolResultSummary, ...],
) -> str:
    if not tool_results:
        return response_text
    evidence = [response_text, "Verified tool context:"]
    for result in tool_results:
        detail = {
            "tool": result.tool_name,
            "summary": result.summary,
            "sql": result.sql,
            "rows": result.rows,
            "total_rows": result.total_rows,
            "artifact_path": result.artifact_path,
        }
        evidence.append(json.dumps(detail, default=str, separators=(",", ":")))
    return "\n".join(evidence)
