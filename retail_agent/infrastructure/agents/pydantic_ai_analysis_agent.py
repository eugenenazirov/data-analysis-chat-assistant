from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart

from retail_agent.agent import ConversationState, run_question
from retail_agent.application.dto import AgentAnalysisResult
from retail_agent.application.ports import (
    AnalyticsGateway,
    ChartCodeExecutor,
    GoldenExampleRepository,
    Telemetry,
)
from retail_agent.domain.models import (
    Conversation,
    ConversationRole,
    QueryResult,
    ToolResultSummary,
    UserProfile,
    UserQuestion,
)
from retail_agent.infrastructure.agents.runner import AnalysisAgentRunner
from retail_agent.infrastructure.settings import ApplicationSettings


class PydanticAIAnalysisAgent:
    def __init__(
        self,
        config: ApplicationSettings,
        analytics: AnalyticsGateway,
        retrieval: GoldenExampleRepository,
        chart_executor: ChartCodeExecutor,
        telemetry: Telemetry,
        runner: AnalysisAgentRunner,
        model_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.config = config
        self.analytics = analytics
        self.retrieval = retrieval
        self.chart_executor = chart_executor
        self.telemetry = telemetry
        self.runner = runner
        self.model_factory = model_factory
        self._analysis_model: Any | None = None

    async def analyze(
        self,
        question: UserQuestion,
        conversation: Conversation,
        user: UserProfile,
    ) -> AgentAnalysisResult:
        legacy_state = ConversationState(
            session_id=str(conversation.id),
            completed_turns=_conversation_history(
                conversation,
                max_groups=self.config.conversation.max_history_turns,
            ),
            completed_query_results=_conversation_query_results(
                conversation,
                max_groups=self.config.conversation.max_history_turns,
            ),
            turn_index=conversation.completed_turn_count,
        )
        turn = await run_question(
            question.root,
            config=self.config,
            bigquery=self.analytics,
            golden_store=self.retrieval,
            chart_executor=self.chart_executor,
            logger=self.telemetry,
            user_id=user.user_id,
            conversation=legacy_state,
            analysis_agent=self.runner,
            analysis_model=self._get_analysis_model(),
        )
        tool_results: list[ToolResultSummary] = []
        if turn.query_result is not None:
            query_result = turn.query_result
            tool_results.append(
                ToolResultSummary(
                    tool_name="run_sql_query",
                    summary=(
                        f"Verified query returned {query_result.total_rows} of "
                        f"{query_result.available_rows} available rows; the result is truncated."
                        if query_result.truncated
                        else f"Verified query returned {query_result.total_rows} rows completely."
                    ),
                    sql=query_result.sql,
                    rows=tuple(query_result.rows[:20]),
                    total_rows=query_result.total_rows,
                    available_rows=query_result.available_rows,
                    truncated=query_result.truncated,
                    row_limit=query_result.row_limit,
                )
            )
        if turn.chart_artifact is not None:
            artifact = turn.chart_artifact
            tool_results.append(
                ToolResultSummary(
                    tool_name="generate_chart",
                    summary=(
                        f"Generated {artifact.output_format.upper()} chart "
                        f"({artifact.size_bytes} bytes)."
                    ),
                    artifact_path=artifact.path,
                )
            )
        return AgentAnalysisResult(
            response=turn.response,
            tool_results=tuple(tool_results),
        )

    def _get_analysis_model(self) -> Any | None:
        if self.model_factory is None:
            return None
        if self._analysis_model is None:
            self._analysis_model = self.model_factory()
        return self._analysis_model

    @property
    def analysis_model(self) -> Any | None:
        """Return the single model instance shared by app and live evaluations."""

        return self._get_analysis_model()


def _conversation_history(
    conversation: Conversation,
    *,
    max_groups: int | None = None,
) -> tuple[tuple[ModelMessage, ...], ...]:
    """Translate persisted domain turns into complete PydanticAI history groups."""

    completed: list[tuple[ModelMessage, ...]] = []
    pending: list[ModelMessage] = []
    turns = conversation.turns
    if max_groups is not None:
        turns = turns[-(max_groups * 2) :]
    for turn in turns:
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


def _conversation_query_results(
    conversation: Conversation,
    *,
    max_groups: int | None = None,
) -> tuple[QueryResult | None, ...]:
    results: list[QueryResult | None] = []
    assistant_turns = [
        turn for turn in conversation.turns if turn.role is ConversationRole.assistant
    ]
    if max_groups is not None:
        assistant_turns = assistant_turns[-max_groups:]
    for turn in assistant_turns:
        query_summary = next(
            (
                result
                for result in turn.tool_result_summaries
                if result.tool_name == "run_sql_query" and result.sql is not None
            ),
            None,
        )
        if query_summary is None:
            results.append(None)
            continue
        results.append(
            QueryResult(
                sql=query_summary.sql,
                rows=list(query_summary.rows),
                total_rows=query_summary.total_rows or len(query_summary.rows),
                available_rows=query_summary.available_rows,
                truncated=query_summary.truncated,
                row_limit=query_summary.row_limit,
            )
        )
    return tuple(results)


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
            "available_rows": result.available_rows,
            "truncated": result.truncated,
            "row_limit": result.row_limit,
            "artifact_path": result.artifact_path,
        }
        evidence.append(json.dumps(detail, default=str, separators=(",", ":")))
    return "\n".join(evidence)
