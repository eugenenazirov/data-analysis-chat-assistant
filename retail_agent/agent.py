from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import (
    Agent,
    FunctionToolset,
    ModelRetry,
    ModelSettings,
    RunContext,
    UsageLimits,
)
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.exceptions import (
    ModelAPIError,
    ToolRetryError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ToolReturnPart,
    UserPromptPart,
)

from retail_agent.bigquery import (
    QueryCostExceeded,
    QueryExecutionError,
    QueryOutcomeUnknownError,
    QueryPreExecutionError,
)
from retail_agent.config import DEFAULT_HISTORY_BYTES, AgentConfig
from retail_agent.domain.policies.report_evidence import assess_report_evidence
from retail_agent.infrastructure.prompts.builder import build_analysis_prompt
from retail_agent.models import (
    AgentFailure,
    AnalysisReport,
    AnalysisResponse,
    AnalysisResult,
    ClarificationRequest,
    DataAnalysisResult,
    ExecutionFailure,
    FailureCode,
    QueryResult,
    RetrievedTrio,
    SchemaExplanationResult,
    UnsupportedRequest,
    UserProfile,
)
from retail_agent.observability import EventLogger, new_trace_id
from retail_agent.pii import redact_value
from retail_agent.ports import AnalysisAgentPort, KnowledgeRetrieverPort, WarehousePort


class GoldenRetrievalResult(BaseModel):
    status: Literal["ok", "degraded"]
    examples: list[RetrievedTrio] = Field(default_factory=list)
    error_code: str | None = None


@dataclass
class AgentDependencies:
    config: AgentConfig
    bigquery: WarehousePort
    logger: EventLogger
    user: UserProfile
    trace_id: str
    golden_store: KnowledgeRetrieverPort | None = None
    last_query_result: QueryResult | None = None
    last_tool_failure_code: FailureCode | None = None
    last_tool_failure_retryable: bool = False
    sql_tool_invoked: bool = False
    retrieval_attempted: bool = False
    retrieval_degraded: bool = False
    retrieved_trio_ids: list[str] = field(default_factory=list)
    tool_sequence: list[str] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    output_validation_retries: int = 0


@dataclass(frozen=True)
class ConversationState:
    """In-memory CLI state; production persists the same turn boundary durably."""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    completed_turns: tuple[tuple[ModelMessage, ...], ...] = ()
    recent_questions: tuple[str, ...] = ()
    turn_index: int = 0

    def message_history(
        self, max_turns: int, max_bytes: int = DEFAULT_HISTORY_BYTES
    ) -> list[ModelMessage]:
        selected: list[tuple[ModelMessage, ...]] = []
        used_bytes = 0
        for turn in reversed(self.completed_turns[-max_turns:]):
            turn_bytes = _message_size(turn)
            if used_bytes + turn_bytes > max_bytes:
                break
            selected.append(turn)
            used_bytes += turn_bytes
        return [message for turn in reversed(selected) for message in turn]

    def complete_turn(
        self,
        *,
        question: str,
        messages: Sequence[ModelMessage],
        max_turns: int,
        max_bytes: int = DEFAULT_HISTORY_BYTES,
    ) -> ConversationState:
        turns = self.completed_turns
        if messages:
            compacted = tuple(_compact_tool_returns(messages, max_bytes))
            turns = (*turns, compacted)[-max_turns:]
        return ConversationState(
            session_id=self.session_id,
            completed_turns=turns,
            recent_questions=(*self.recent_questions, question)[-max_turns:],
            turn_index=self.turn_index + 1,
        )

    def fail_turn(
        self,
        *,
        question: str,
        max_turns: int,
        max_bytes: int = DEFAULT_HISTORY_BYTES,
    ) -> ConversationState:
        return self.complete_turn(
            question=question,
            messages=(),
            max_turns=max_turns,
            max_bytes=max_bytes,
        )


@dataclass(frozen=True)
class TurnResult:
    response: AnalysisResponse
    conversation: ConversationState
    retrieved_trio_ids: tuple[str, ...] = ()
    query_result: QueryResult | None = None
    sql_tool_invoked: bool = False


async def add_runtime_context(ctx: RunContext[AgentDependencies]) -> str:
    schema = await asyncio.to_thread(ctx.deps.bigquery.describe_allowed_tables)
    return (
        f"Trace ID: {ctx.deps.trace_id}\n"
        f"User: {ctx.deps.user.display_name}\n"
        f"Preferred report format: {ctx.deps.user.preferred_format}\n"
        f"Tone: {ctx.deps.user.tone}\n"
        f"Allowed BigQuery tables:\n{schema}\n"
        "Use fully qualified BigQuery table names and backticks."
    )


async def retrieve_golden_examples(
    ctx: RunContext[AgentDependencies],
    question: str,
    limit: Annotated[int | None, Field(ge=1, le=20)] = None,
) -> GoldenRetrievalResult:
    """Retrieve analyst-approved examples for a standalone, context-resolved question."""

    started = time.perf_counter()
    ctx.deps.retrieval_attempted = True
    ctx.deps.tool_sequence.append("retrieve_golden_examples")
    selected_limit = min(limit or ctx.deps.config.retrieval.top_k, 20)
    if ctx.deps.golden_store is None:
        ctx.deps.retrieval_degraded = True
        _record_tool_event(ctx, "retrieve_golden_examples", "degraded", started)
        return GoldenRetrievalResult(
            status="degraded",
            error_code="retrieval_not_configured",
        )
    try:
        examples = await asyncio.to_thread(
            ctx.deps.golden_store.search,
            question.strip(),
            ctx.deps.trace_id,
            selected_limit,
        )
    except Exception as exc:
        ctx.deps.retrieval_degraded = True
        ctx.deps.logger.event(
            ctx.deps.trace_id,
            "golden_knowledge_unavailable",
            failure_class=exc.__class__.__name__,
            error=str(exc),
        )
        _record_tool_event(ctx, "retrieve_golden_examples", "degraded", started)
        return GoldenRetrievalResult(
            status="degraded",
            error_code="retrieval_unavailable",
        )

    ctx.deps.retrieved_trio_ids = [example.id for example in examples]
    _record_tool_event(ctx, "retrieve_golden_examples", "succeeded", started)
    return GoldenRetrievalResult(status="ok", examples=examples)


async def run_sql_query(ctx: RunContext[AgentDependencies], sql: str) -> QueryResult:
    """Validate and run a read-only BigQuery SQL query."""

    started = time.perf_counter()
    ctx.deps.sql_tool_invoked = True
    ctx.deps.tool_sequence.append("run_sql_query")
    try:
        result = await asyncio.to_thread(
            ctx.deps.bigquery.execute,
            sql,
            ctx.deps.trace_id,
        )
        if result.total_rows == 0:
            _record_tool_event(ctx, "run_sql_query", "empty", started)
            _record_tool_failure(ctx, "retry_exhausted", retryable=False)
            _log_sql_retry_feedback(
                ctx,
                failure_class="EmptyResult",
                error="The query returned no rows.",
            )
            raise ModelRetry(
                "The query returned no rows. Revise the SQL once using broader "
                "filters or explain why no matching data exists."
            )
        ctx.deps.last_query_result = result
        ctx.deps.last_tool_failure_code = None
        ctx.deps.last_tool_failure_retryable = False
        _record_tool_event(ctx, "run_sql_query", "succeeded", started)
        return result
    except QueryCostExceeded as exc:
        _record_tool_event(ctx, "run_sql_query", "rejected", started)
        _record_tool_failure(ctx, "retry_exhausted", retryable=False)
        _log_sql_retry_feedback(ctx, failure_class=exc.__class__.__name__, error=str(exc))
        raise ModelRetry(str(exc)) from exc
    except QueryOutcomeUnknownError as exc:
        _record_tool_event(ctx, "run_sql_query", "failed", started)
        _record_tool_failure(ctx, "warehouse_outcome_unknown", retryable=False)
        ctx.deps.logger.event(
            ctx.deps.trace_id,
            "sql_terminal_failure",
            failure_class=exc.__class__.__name__,
            failure_code="warehouse_outcome_unknown",
            job_id=exc.job_id,
            error=str(exc),
        )
        raise
    except QueryPreExecutionError as exc:
        _record_tool_event(ctx, "run_sql_query", "failed", started)
        _record_tool_failure(ctx, "warehouse_unavailable", retryable=True)
        _log_sql_retry_feedback(ctx, failure_class=exc.__class__.__name__, error=str(exc))
        raise ModelRetry(str(exc)) from exc
    except ValueError as exc:
        _record_tool_event(ctx, "run_sql_query", "rejected", started)
        _record_tool_failure(ctx, "retry_exhausted", retryable=False)
        _log_sql_retry_feedback(ctx, failure_class=exc.__class__.__name__, error=str(exc))
        raise ModelRetry(str(exc)) from exc


def build_analysis_toolset(config: AgentConfig) -> FunctionToolset[AgentDependencies]:
    toolset = FunctionToolset[AgentDependencies](
        instructions=(
            "Choose tools based on the question. Retrieval is optional, but when used its "
            "question must be standalone and resolve conversation references. Data answers "
            "must execute run_sql_query and use only its verified result."
        ),
        sequential=True,
    )
    toolset.add_function(
        retrieve_golden_examples,
        retries=0,
        timeout=config.retrieval.timeout_seconds,
    )
    toolset.add_function(
        run_sql_query,
        retries=config.agent_limits.max_sql_retries,
        timeout=config.bigquery.timeout_seconds + 5,
    )
    return toolset


def build_analysis_agent(config: AgentConfig) -> Agent:
    """Build the bounded PydanticAI analysis agent and analytics toolset."""

    prompt = build_analysis_prompt()
    analysis_agent = Agent(
        model=None,
        deps_type=AgentDependencies,
        output_type=AnalysisResult,
        instructions=prompt.instructions,
        retries={"output": config.agent_limits.output_retries},
        toolsets=[build_analysis_toolset(config)],
        model_settings=ModelSettings(temperature=config.model.temperature),
        capabilities=[ProcessHistory(_process_message_history)],
        defer_model_check=True,
    )
    analysis_agent.instructions(add_runtime_context)

    @analysis_agent.output_validator
    async def validate_output(
        ctx: RunContext[AgentDependencies],
        output: AnalysisResult,
    ) -> AnalysisResult:
        if not isinstance(output, DataAnalysisResult):
            return output
        query_result = ctx.deps.last_query_result
        if query_result is None:
            ctx.deps.output_validation_retries = ctx.retry + 1
            ctx.deps.logger.event(
                ctx.deps.trace_id,
                "output_validation_retry",
                reason="data_answer_without_query",
                retry_attempt=ctx.retry,
                max_retries=ctx.max_retries,
            )
            raise ModelRetry("A data analysis result requires a successful run_sql_query call.")
        report = _data_result_to_report("", output, query_result)
        evidence = assess_report_evidence(
            report,
            query_result.rows,
            query_result.sql,
        )
        if not evidence.is_supported:
            ctx.deps.output_validation_retries = ctx.retry + 1
            ctx.deps.logger.event(
                ctx.deps.trace_id,
                "output_validation_retry",
                reason="unsupported_numeric_claim",
                retry_attempt=ctx.retry,
                max_retries=ctx.max_retries,
                unsupported_claim_count=len(evidence.unsupported_numeric_claims),
            )
            raise ModelRetry(
                "Revise the answer so every numeric claim is supported by the verified "
                "query result."
            )
        return output

    return analysis_agent


async def run_question(
    question: str,
    *,
    config: AgentConfig,
    bigquery: WarehousePort,
    golden_store: KnowledgeRetrieverPort,
    logger: EventLogger,
    user_id: str,
    conversation: ConversationState | None = None,
    analysis_agent: AnalysisAgentPort | None = None,
) -> TurnResult:
    state = conversation or ConversationState()
    turn_index = state.turn_index + 1
    trace_id = new_trace_id()
    started = time.perf_counter()
    user = config.user_profile(user_id)
    history = state.message_history(
        config.conversation.max_history_turns,
        config.conversation.max_history_bytes,
    )
    logger.event(
        trace_id,
        "agent_run_started",
        user_id=user_id,
        question=question,
        session_id=state.session_id,
        turn_index=turn_index,
        history_messages=len(history),
        model=config.model.llm_model,
        prompt_version=build_analysis_prompt().version,
        persona_version=config.persona_version,
        golden_index_version=config.retrieval.collection,
    )

    deps = AgentDependencies(
        config=config,
        bigquery=bigquery,
        logger=logger,
        user=user,
        trace_id=trace_id,
        golden_store=golden_store,
    )

    prompt = (
        f"User question: {question}\n"
        "Use the bounded conversation history to resolve follow-up references. "
        "Call only the tools needed for this question.\n"
        f"Return a report matching preferred format {user.preferred_format}."
    )
    runner = analysis_agent or build_analysis_agent(config)
    try:
        result = await runner.run(
            prompt,
            deps=deps,
            model=config.model.llm_model,
            message_history=history or None,
            conversation_id=state.session_id,
            model_settings=ModelSettings(temperature=config.model.temperature),
            usage_limits=_usage_limits(config),
        )
    except Exception as exc:
        failure_code, retryable = _classify_failure(
            exc,
            tool_failure_code=deps.last_tool_failure_code,
            tool_failure_retryable=deps.last_tool_failure_retryable,
        )
        degraded = deps.last_query_result is not None
        logger.event(
            trace_id,
            "agent_run_failed",
            session_id=state.session_id,
            turn_index=turn_index,
            phase="model_run",
            failure_class=exc.__class__.__name__,
            failure_code=failure_code,
            retryable=retryable,
            degraded=degraded,
            model=config.model.llm_model,
            prompt_version=build_analysis_prompt().version,
            persona_version=config.persona_version,
            golden_index_version=config.retrieval.collection,
            retrieval_attempted=deps.retrieval_attempted,
            retrieval_degraded=deps.retrieval_degraded,
            tool_sequence=deps.tool_sequence,
            tool_events=deps.tool_events,
            output_validation_retries=deps.output_validation_retries,
            duration_ms=_duration_ms(started),
            error=str(exc),
        )
        next_state = state.fail_turn(
            question=question,
            max_turns=config.conversation.max_history_turns,
            max_bytes=config.conversation.max_history_bytes,
        )
        if deps.last_query_result is not None:
            response: AnalysisResponse = _build_degraded_report(
                question, deps.last_query_result, trace_id
            )
        else:
            response = AgentFailure(
                question=question,
                message=_failure_message(failure_code),
                failure_code=failure_code,
                trace_id=trace_id,
                retryable=retryable,
            )
        return TurnResult(
            response=response,
            conversation=next_state,
            retrieved_trio_ids=tuple(deps.retrieved_trio_ids),
            query_result=deps.last_query_result,
            sql_tool_invoked=deps.sql_tool_invoked,
        )

    response = _to_analysis_response(
        question,
        result.output,
        deps,
        trace_id,
    )
    new_messages = _new_messages(result)
    next_state = state.complete_turn(
        question=question,
        messages=new_messages,
        max_turns=config.conversation.max_history_turns,
        max_bytes=config.conversation.max_history_bytes,
    )
    logger.event(
        trace_id,
        "agent_run_completed",
        session_id=state.session_id,
        turn_index=turn_index,
        refused=isinstance(response, AnalysisReport) and response.refused,
        degraded=isinstance(response, AnalysisReport) and response.degraded,
        sql=response.sql if isinstance(response, AnalysisReport) else None,
        retrieved_trio_ids=deps.retrieved_trio_ids,
        retrieval_attempted=deps.retrieval_attempted,
        retrieval_degraded=deps.retrieval_degraded,
        tool_sequence=deps.tool_sequence,
        tool_events=deps.tool_events,
        usage=_usage(result),
        output_validation_retries=deps.output_validation_retries,
        duration_ms=_duration_ms(started),
        prompt_version=build_analysis_prompt().version,
        persona_version=config.persona_version,
        golden_index_version=config.retrieval.collection,
    )
    return TurnResult(
        response=response,
        conversation=next_state,
        retrieved_trio_ids=tuple(deps.retrieved_trio_ids),
        query_result=deps.last_query_result,
        sql_tool_invoked=deps.sql_tool_invoked,
    )


def _log_sql_retry_feedback(
    ctx: RunContext[AgentDependencies], *, failure_class: str, error: str
) -> None:
    ctx.deps.logger.event(
        ctx.deps.trace_id,
        "sql_retry_feedback",
        failure_class=failure_class,
        retry_attempt=ctx.retry,
        max_retries=ctx.max_retries,
        configured_retry_budget=ctx.deps.config.agent_limits.max_sql_retries,
        error=error,
    )


def _record_tool_failure(
    ctx: RunContext[AgentDependencies],
    failure_code: FailureCode,
    *,
    retryable: bool,
) -> None:
    ctx.deps.last_tool_failure_code = failure_code
    ctx.deps.last_tool_failure_retryable = retryable


def _to_analysis_response(
    question: str,
    output: Any,
    deps: AgentDependencies,
    trace_id: str,
) -> AnalysisResponse:
    if isinstance(output, AnalysisReport):
        report = output
        if deps.last_query_result is not None:
            report = report.model_copy(update={"sql": deps.last_query_result.sql})
        return _sanitize_report(report, trace_id)
    if isinstance(output, DataAnalysisResult):
        if deps.last_query_result is None:
            return AgentFailure(
                question=question,
                message=_failure_message("retry_exhausted"),
                failure_code="retry_exhausted",
                trace_id=trace_id,
                retryable=False,
            )
        return _sanitize_report(
            _data_result_to_report(question, output, deps.last_query_result),
            trace_id,
        )
    if isinstance(output, SchemaExplanationResult):
        return _sanitize_report(
            AnalysisReport(
                question=question,
                answer=output.explanation,
                caveats=output.caveats,
                followups=output.followups,
            ),
            trace_id,
        )
    if isinstance(output, ClarificationRequest):
        return _sanitize_report(
            AnalysisReport(question=question, answer=output.question),
            trace_id,
        )
    if isinstance(output, UnsupportedRequest):
        return _sanitize_report(
            AnalysisReport(
                question=question,
                answer=output.reason,
                refused=True,
            ),
            trace_id,
        )
    if isinstance(output, ExecutionFailure):
        failure_code: FailureCode = "model_unavailable" if output.retryable else "internal_error"
        return AgentFailure(
            question=question,
            message=_failure_message(failure_code),
            failure_code=failure_code,
            trace_id=trace_id,
            retryable=output.retryable,
        )
    raise TypeError(f"Unsupported analysis output: {type(output)!r}")


def _data_result_to_report(
    question: str,
    output: DataAnalysisResult,
    query_result: QueryResult,
) -> AnalysisReport:
    return AnalysisReport(
        question=question,
        answer=output.direct_answer,
        highlights=[*output.highlights, *output.supporting_evidence],
        table=query_result.rows,
        sql=query_result.sql,
        caveats=output.caveats,
        followups=output.followups,
    )


def _usage_limits(config: AgentConfig) -> UsageLimits:
    return UsageLimits(
        request_limit=config.agent_limits.request_limit,
        tool_calls_limit=config.agent_limits.tool_calls_limit,
        total_tokens_limit=config.agent_limits.total_tokens_limit,
    )


def _process_message_history(
    ctx: RunContext[AgentDependencies],
    messages: list[ModelMessage],
) -> list[ModelMessage]:
    groups: list[list[ModelMessage]] = []
    current: list[ModelMessage] = []
    for message in messages:
        starts_turn = isinstance(message, ModelRequest) and any(
            isinstance(part, UserPromptPart) for part in message.parts
        )
        if starts_turn and current:
            groups.append(current)
            current = []
        current.append(message)
    if current:
        groups.append(current)

    selected: list[list[ModelMessage]] = []
    used_bytes = 0
    max_groups = ctx.deps.config.conversation.max_history_turns + 1
    for group in reversed(groups[-max_groups:]):
        group_bytes = _message_size(group)
        if selected and used_bytes + group_bytes > ctx.deps.config.conversation.max_history_bytes:
            break
        selected.append(group)
        used_bytes += group_bytes
    return [message for group in reversed(selected) for message in group]


def _record_tool_event(
    ctx: RunContext[AgentDependencies],
    tool_name: str,
    status: str,
    started: float,
) -> None:
    event = {
        "tool": tool_name,
        "status": status,
        "duration_ms": _duration_ms(started),
    }
    ctx.deps.tool_events.append(event)
    ctx.deps.logger.event(ctx.deps.trace_id, "agent_tool_completed", **event)


def _sanitize_report(report: AnalysisReport, trace_id: str) -> AnalysisReport:
    data: dict[str, Any] = report.model_dump()
    redacted, _ = redact_value(data)
    redacted["trace_id"] = trace_id
    return AnalysisReport.model_validate(redacted)


def _build_degraded_report(
    question: str, query_result: QueryResult, trace_id: str
) -> AnalysisReport:
    return _sanitize_report(
        AnalysisReport(
            question=question,
            answer=(
                "The narrative model became unavailable after the data query completed. "
                "The verified query results are shown below."
            ),
            table=query_result.rows,
            sql=query_result.sql,
            caveats=["Narrative interpretation is unavailable; retry for a full report."],
            degraded=True,
        ),
        trace_id,
    )


def _classify_failure(
    exc: Exception,
    *,
    tool_failure_code: FailureCode | None = None,
    tool_failure_retryable: bool = False,
) -> tuple[FailureCode, bool]:
    if isinstance(exc, QueryOutcomeUnknownError):
        return "warehouse_outcome_unknown", False
    if isinstance(exc, QueryExecutionError):
        return "warehouse_unavailable", True
    if isinstance(exc, (ToolRetryError, UnexpectedModelBehavior)):
        if tool_failure_code is not None:
            return tool_failure_code, tool_failure_retryable
        return "retry_exhausted", False
    if isinstance(exc, UsageLimitExceeded):
        return "model_unavailable", False
    if isinstance(exc, (ModelAPIError, ConnectionError, TimeoutError)):
        return "model_unavailable", True
    return "internal_error", False


def _failure_message(failure_code: FailureCode) -> str:
    return {
        "model_unavailable": (
            "The analysis model is temporarily unavailable. Please retry shortly."
        ),
        "warehouse_unavailable": (
            "The analytics warehouse is temporarily unavailable. Please retry shortly."
        ),
        "warehouse_outcome_unknown": (
            "The warehouse did not confirm whether the query completed. Do not retry "
            "immediately; contact support with the trace ID so the original job can be "
            "checked."
        ),
        "retry_exhausted": (
            "I could not produce a safe, valid analysis within the retry budget. "
            "Try narrowing the question."
        ),
        "internal_error": (
            "I could not complete the analysis safely. Please retry or contact support "
            "with the trace ID."
        ),
    }[failure_code]


def _new_messages(result: Any) -> list[ModelMessage]:
    value = getattr(result, "new_messages", None)
    if callable(value):
        return list(value())
    return []


def _usage(result: Any) -> Any:
    value = getattr(result, "usage", None)
    return value() if callable(value) else value


def _duration_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _compact_tool_returns(messages: Sequence[ModelMessage], max_bytes: int) -> list[ModelMessage]:
    materialized = list(messages)
    if _message_size(materialized) <= max_bytes:
        return materialized

    compacted: list[ModelMessage] = []
    for message in materialized:
        if not isinstance(message, ModelRequest):
            compacted.append(message)
            continue
        parts = [
            replace(
                part,
                content={
                    "summary": (
                        "Verified SQL rows omitted from conversation history to keep "
                        "the context bounded; rerun the query if exact rows are needed."
                    )
                },
            )
            if isinstance(part, ToolReturnPart)
            else part
            for part in message.parts
        ]
        compacted.append(replace(message, parts=parts))
    return compacted


def _message_size(messages: Sequence[ModelMessage]) -> int:
    return len(ModelMessagesTypeAdapter.dump_json(list(messages)))
