from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import (
    Agent,
    FunctionToolset,
    ModelRetry,
    ModelSettings,
    RunContext,
    ToolDefinition,
    UsageLimits,
)
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
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

from retail_agent.application.ports import (
    AnalyticsGateway,
    ChartCodeExecutor,
    GoldenExampleRepository,
    Telemetry,
)
from retail_agent.bigquery import (
    QueryCostExceeded,
    QueryExecutionError,
    QueryOutcomeUnknownError,
    QueryPreExecutionError,
)
from retail_agent.config import DEFAULT_HISTORY_BYTES, AgentConfig
from retail_agent.domain.errors import ChartExecutionError, ChartExecutionFailureCode
from retail_agent.domain.policies.analysis_output import (
    narrative_output_violation,
)
from retail_agent.domain.policies.query_semantics import validate_query_semantics
from retail_agent.domain.policies.report_evidence import assess_report_evidence
from retail_agent.domain.policies.request_routing import classify_non_query_request
from retail_agent.domain.policies.retrieval import (
    RETRIEVAL_ROUTING_RULE,
    is_schema_question,
    requires_golden_precedent,
)
from retail_agent.infrastructure.agents.runner import AnalysisAgentRunner
from retail_agent.infrastructure.prompts.builder import build_analysis_prompt
from retail_agent.models import (
    AgentFailure,
    AnalysisReport,
    AnalysisResponse,
    AnalysisResult,
    ChartArtifact,
    ChartFormat,
    ChartRequest,
    ClarificationRequest,
    DataAnalysisResult,
    ExecutionFailure,
    FailureCode,
    OperationalMetrics,
    QueryResult,
    RetrievedTrio,
    SchemaExplanationResult,
    UnsupportedRequest,
    UserProfile,
)
from retail_agent.observability import new_trace_id
from retail_agent.pii import redact_value


class GoldenRetrievalResult(BaseModel):
    status: Literal["ok", "degraded"]
    examples: list[RetrievedTrio] = Field(default_factory=list)
    error_code: str | None = None


class QueryToolResult(BaseModel):
    """Bounded query evidence returned to the model.

    The complete verified result stays in ``AgentDependencies.last_query_result`` for
    the final table, evidence validation, and chart input. Sending only a typed preview
    here prevents a wide but complete result from consuming the next model turn's token
    budget.
    """

    columns: list[str]
    preview_rows: list[dict[str, Any]]
    preview_row_count: int = Field(ge=0)
    returned_rows: int = Field(ge=0)
    available_rows: int = Field(ge=0)
    truncated: bool
    row_limit: int | None = Field(default=None, ge=1)
    guidance: str = (
        "preview_rows is bounded model context; the runtime retains and attaches all "
        "returned rows. Treat the result as incomplete only when truncated is true."
    )


class ChartToolResult(BaseModel):
    status: Literal["ok", "error"]
    artifact: ChartArtifact | None = None
    error_code: str | None = None
    repair_hint: str | None = None


type ChartFailureCode = (
    ChartExecutionFailureCode
    | Literal[
        "executor_unavailable",
        "not_generated",
        "truncated_query",
        "verified_query_required",
    ]
)

_CHART_REQUEST_PATTERN = re.compile(
    r"\b(?:chart|graph|plot|visuali[sz](?:ation|e)?)\b",
    re.IGNORECASE,
)


@dataclass
class AgentDependencies:
    config: AgentConfig
    bigquery: AnalyticsGateway
    logger: Telemetry
    user: UserProfile
    trace_id: str
    question: str = ""
    reference_date: date = field(default_factory=lambda: datetime.now(UTC).date())
    golden_store: GoldenExampleRepository | None = None
    chart_executor: ChartCodeExecutor | None = None
    last_query_result: QueryResult | None = None
    prior_query_results: tuple[QueryResult, ...] = ()
    last_tool_failure_code: FailureCode | None = None
    last_tool_failure_retryable: bool = False
    sql_tool_invoked: bool = False
    retrieval_attempted: bool = False
    retrieval_required: bool = False
    retrieval_degraded: bool = False
    schema_only: bool = False
    chart_requested: bool = False
    retrieved_trio_ids: list[str] = field(default_factory=list)
    retrieved_trios: list[RetrievedTrio] = field(default_factory=list)
    tool_sequence: list[str] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    output_validation_retries: int = 0
    last_chart_artifact: ChartArtifact | None = None
    last_chart_failure_code: ChartFailureCode | None = None
    chart_attempts: int = 0
    sql_retry_reasons: list[str] = field(default_factory=list)
    output_retry_reasons: list[str] = field(default_factory=list)
    bigquery_job_ids: list[str] = field(default_factory=list)
    bigquery_executions: int = 0
    dry_run_bytes: int = 0
    billed_bytes: int = 0
    cache_hits: list[bool] = field(default_factory=list)
    bigquery_dry_runs: int = 0
    model_behavior_retries: int = 0
    failed_provider_requests: int = 0
    model_attempt_tool_event_offset: int = 0


@dataclass(frozen=True)
class ConversationState:
    """In-memory CLI state; production persists the same turn boundary durably."""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    completed_turns: tuple[tuple[ModelMessage, ...], ...] = ()
    completed_query_results: tuple[QueryResult | None, ...] = ()
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
        messages: Sequence[ModelMessage],
        query_result: QueryResult | None = None,
        max_turns: int,
        max_bytes: int = DEFAULT_HISTORY_BYTES,
    ) -> ConversationState:
        turns = self.completed_turns
        if messages:
            compacted = tuple(_compact_tool_returns(messages, max_bytes))
            turns = (*turns, compacted)[-max_turns:]
        query_results = (*self.completed_query_results, query_result)[-max_turns:]
        return ConversationState(
            session_id=self.session_id,
            completed_turns=turns,
            completed_query_results=query_results,
            turn_index=self.turn_index + 1,
        )

    def fail_turn(
        self,
        *,
        query_result: QueryResult | None = None,
        max_turns: int,
        max_bytes: int = DEFAULT_HISTORY_BYTES,
    ) -> ConversationState:
        return self.complete_turn(
            messages=(),
            query_result=query_result,
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
    chart_artifact: ChartArtifact | None = None
    reference_date: date | None = None
    trace_id: str | None = None
    operational: OperationalMetrics = field(default_factory=OperationalMetrics)


async def add_runtime_context(ctx: RunContext[AgentDependencies]) -> str:
    schema = await asyncio.to_thread(ctx.deps.bigquery.describe_allowed_tables)
    return (
        f"Trace ID: {ctx.deps.trace_id}\n"
        f"Current UTC date: {ctx.deps.reference_date.isoformat()}\n"
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
    """Retrieve approved metric, cohort, join, filter, and time-window precedent."""

    return await _retrieve_golden_context(
        ctx.deps,
        question=question,
        limit=limit,
        retry=getattr(ctx, "retry", 0),
        max_retries=getattr(ctx, "max_retries", 0),
    )


async def _retrieve_golden_context(
    deps: AgentDependencies,
    *,
    question: str,
    limit: int | None = None,
    retry: int = 0,
    max_retries: int = 0,
) -> GoldenRetrievalResult:
    """Run one bounded retrieval, whether orchestrated by the app or the model."""

    started = time.perf_counter()
    deps.retrieval_attempted = True
    deps.tool_sequence.append("retrieve_golden_examples")
    selected_limit = min(limit or deps.config.retrieval.top_k, 20)
    if deps.golden_store is None:
        deps.retrieval_degraded = True
        _record_dependency_tool_event(deps, "retrieve_golden_examples", "degraded", started)
        return GoldenRetrievalResult(
            status="degraded",
            error_code="retrieval_not_configured",
        )
    try:
        examples = await asyncio.to_thread(
            deps.golden_store.search,
            question.strip(),
            deps.trace_id,
            selected_limit,
        )
    except Exception as exc:
        deps.retrieval_degraded = True
        deps.logger.event(
            deps.trace_id,
            "golden_knowledge_unavailable",
            failure_class=exc.__class__.__name__,
            retry_attempt=retry,
            max_retries=max_retries,
        )
        _record_dependency_tool_event(
            deps,
            "retrieve_golden_examples",
            "degraded",
            started,
        )
        return GoldenRetrievalResult(
            status="degraded",
            error_code="retrieval_unavailable",
        )

    deps.retrieved_trios = list(examples)
    deps.retrieved_trio_ids = [example.id for example in examples]
    _record_dependency_tool_event(
        deps,
        "retrieve_golden_examples",
        "succeeded",
        started,
    )
    return GoldenRetrievalResult(status="ok", examples=examples)


async def run_sql_query(ctx: RunContext[AgentDependencies], sql: str) -> QueryToolResult:
    """Validate and run a read-only BigQuery SQL query."""

    started = time.perf_counter()
    ctx.deps.sql_tool_invoked = True
    ctx.deps.tool_sequence.append("run_sql_query")
    try:
        validate_query_semantics(
            sql,
            question=ctx.deps.question,
            reference_date=ctx.deps.reference_date,
            prior_sql=(
                ctx.deps.prior_query_results[-1].sql if ctx.deps.prior_query_results else None
            ),
        )
        result = await asyncio.to_thread(
            ctx.deps.bigquery.execute,
            sql,
            ctx.deps.trace_id,
        )
        ctx.deps.bigquery_dry_runs += 1
        ctx.deps.bigquery_executions += 1
        if result.job_id is not None:
            ctx.deps.bigquery_job_ids.append(result.job_id)
        ctx.deps.dry_run_bytes += result.dry_run_bytes or 0
        ctx.deps.billed_bytes += result.total_bytes_billed or 0
        if result.cache_hit is not None:
            ctx.deps.cache_hits.append(result.cache_hit)
        ctx.deps.last_query_result = result
        ctx.deps.last_tool_failure_code = None
        ctx.deps.last_tool_failure_retryable = False
        _record_tool_event(
            ctx,
            "run_sql_query",
            "empty" if result.total_rows == 0 else "succeeded",
            started,
        )
        return _query_tool_result(result)
    except QueryCostExceeded as exc:
        ctx.deps.bigquery_dry_runs += 1
        _record_tool_event(ctx, "run_sql_query", "rejected", started)
        _record_tool_failure(ctx, "retry_exhausted", retryable=False)
        _log_sql_retry_feedback(ctx, failure_class=exc.__class__.__name__, error=str(exc))
        raise ModelRetry(str(exc)) from exc
    except QueryOutcomeUnknownError as exc:
        ctx.deps.bigquery_dry_runs += 1
        ctx.deps.bigquery_executions += 1
        _record_tool_event(ctx, "run_sql_query", "failed", started)
        _record_tool_failure(ctx, "warehouse_outcome_unknown", retryable=False)
        ctx.deps.bigquery_job_ids.append(exc.job_id)
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
        ctx.deps.bigquery_dry_runs += 1
        _record_tool_event(ctx, "run_sql_query", "failed", started)
        _record_tool_failure(ctx, "warehouse_unavailable", retryable=True)
        _log_sql_retry_feedback(ctx, failure_class=exc.__class__.__name__, error=str(exc))
        raise ModelRetry(str(exc)) from exc
    except ValueError as exc:
        _record_tool_event(ctx, "run_sql_query", "rejected", started)
        _record_tool_failure(ctx, "retry_exhausted", retryable=False)
        _log_sql_retry_feedback(ctx, failure_class=exc.__class__.__name__, error=str(exc))
        raise ModelRetry(str(exc)) from exc


def _query_tool_result(result: QueryResult, *, preview_limit: int = 10) -> QueryToolResult:
    preview_rows, _ = redact_value(result.rows[:preview_limit])
    columns = sorted({column for row in result.rows for column in row})
    return QueryToolResult(
        columns=columns,
        preview_rows=preview_rows,
        preview_row_count=len(preview_rows),
        returned_rows=result.total_rows,
        available_rows=(
            result.available_rows if result.available_rows is not None else result.total_rows
        ),
        truncated=result.truncated,
        row_limit=result.row_limit,
    )


async def prepare_chart_tool(
    ctx: RunContext[AgentDependencies],
    tool_definition: ToolDefinition,
) -> ToolDefinition | None:
    if (
        not ctx.deps.schema_only
        and ctx.deps.last_query_result is not None
        and not ctx.deps.last_query_result.truncated
        and ctx.deps.chart_executor is not None
        and ctx.deps.chart_attempts <= ctx.deps.config.agent_limits.max_chart_retries
    ):
        return tool_definition
    return None


async def prepare_retrieval_tool(
    ctx: RunContext[AgentDependencies],
    tool_definition: ToolDefinition,
) -> ToolDefinition | None:
    return None if ctx.deps.schema_only or ctx.deps.retrieval_attempted else tool_definition


async def prepare_sql_tool(
    ctx: RunContext[AgentDependencies],
    tool_definition: ToolDefinition,
) -> ToolDefinition | None:
    if (
        ctx.deps.schema_only
        or ctx.deps.last_query_result is not None
        or (ctx.deps.retrieval_required and not ctx.deps.retrieval_attempted)
    ):
        return None
    return tool_definition


async def generate_chart(
    ctx: RunContext[AgentDependencies],
    code: str,
    output_format: ChartFormat = "png",
) -> ChartToolResult:
    """Execute chart Python against input.json and create chart.png or chart.svg.

    Read only the verified rows from input.json in the current working directory.
    Save exactly one artifact using the fixed filename for the selected format.
    """

    started = time.perf_counter()
    retry_attempt = getattr(ctx, "retry", 0)
    max_retries = getattr(ctx, "max_retries", 0)
    ctx.deps.chart_attempts += 1
    ctx.deps.tool_sequence.append("generate_chart")
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if ctx.deps.last_query_result is None:
        _record_tool_event(ctx, "generate_chart", "rejected", started)
        error_code: ChartFailureCode = "verified_query_required"
        ctx.deps.last_chart_artifact = None
        ctx.deps.last_chart_failure_code = error_code
        return ChartToolResult(status="error", error_code=error_code)
    if ctx.deps.last_query_result.truncated:
        _record_tool_event(ctx, "generate_chart", "rejected", started)
        error_code = "truncated_query"
        ctx.deps.last_chart_artifact = None
        ctx.deps.last_chart_failure_code = error_code
        return ChartToolResult(
            status="error",
            error_code=error_code,
            repair_hint="Narrow the query until all rows fit within the result limit.",
        )
    if ctx.deps.chart_executor is None:
        _record_tool_event(ctx, "generate_chart", "unavailable", started)
        error_code = "executor_unavailable"
        ctx.deps.last_chart_artifact = None
        ctx.deps.last_chart_failure_code = error_code
        return ChartToolResult(status="error", error_code=error_code)

    redacted_rows, redaction_count = redact_value(ctx.deps.last_query_result.rows)
    request = ChartRequest(
        code=code,
        data=redacted_rows,
        output_format=output_format,
    )
    try:
        artifact = await ctx.deps.chart_executor.execute(request)
    except ChartExecutionError as exc:
        _record_tool_event(ctx, "generate_chart", "failed", started)
        ctx.deps.last_chart_artifact = None
        ctx.deps.last_chart_failure_code = exc.code
        ctx.deps.logger.event(
            ctx.deps.trace_id,
            "chart_execution_failed",
            code_digest=digest,
            error_code=exc.code,
            retry_attempt=retry_attempt,
            max_retries=max_retries,
        )
        repair_hint = exc.repair_hint or (f"Fix the chart program and save chart.{output_format}.")
        if retry_attempt < max_retries:
            raise ModelRetry(repair_hint) from exc
        return ChartToolResult(
            status="error",
            error_code=exc.code,
            repair_hint=repair_hint,
        )

    ctx.deps.last_chart_artifact = artifact
    ctx.deps.last_chart_failure_code = None
    _record_tool_event(ctx, "generate_chart", "succeeded", started)
    ctx.deps.logger.event(
        ctx.deps.trace_id,
        "chart_execution_completed",
        code_digest=artifact.code_digest,
        output_format=artifact.output_format,
        size_bytes=artifact.size_bytes,
        artifact_path=artifact.path,
        input_redactions=redaction_count,
    )
    return ChartToolResult(status="ok", artifact=artifact)


def build_analysis_toolset(config: AgentConfig) -> FunctionToolset[AgentDependencies]:
    toolset = FunctionToolset[AgentDependencies](
        instructions=(
            f"{RETRIEVAL_ROUTING_RULE}\n\n"
            "Tool mechanics: run_sql_query returns bounded preview_rows from the only "
            "verified current result; the runtime retains and attaches the complete table. "
            "generate_chart appears only after a successful query and reads the complete "
            "verified rows from input.json."
        ),
        sequential=True,
    )
    toolset.add_function(
        retrieve_golden_examples,
        retries=0,
        prepare=prepare_retrieval_tool,
        timeout=config.retrieval.timeout_seconds,
    )
    toolset.add_function(
        run_sql_query,
        retries=config.agent_limits.max_sql_retries,
        prepare=prepare_sql_tool,
        timeout=config.bigquery.timeout_seconds + 5,
    )
    toolset.add_function(
        generate_chart,
        retries=config.agent_limits.max_chart_retries,
        prepare=prepare_chart_tool,
        timeout=config.chart_execution.timeout_seconds + 5,
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
        model_settings=_analysis_model_settings(config),
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
            ctx.deps.output_retry_reasons.append("data_answer_without_query")
            ctx.deps.logger.event(
                ctx.deps.trace_id,
                "output_validation_retry",
                reason="data_answer_without_query",
                retry_attempt=ctx.retry,
                max_retries=ctx.max_retries,
            )
            raise ModelRetry("A data analysis result requires a successful run_sql_query call.")
        if query_result.truncated:
            return output
        output = output.model_copy(update={"highlights": output.highlights[:2]})
        if (
            ctx.deps.chart_requested
            and ctx.deps.chart_executor is not None
            and ctx.deps.last_chart_artifact is None
            and ctx.deps.last_chart_failure_code is None
        ):
            ctx.deps.output_validation_retries = ctx.retry + 1
            ctx.deps.output_retry_reasons.append("chart_required")
            ctx.deps.logger.event(
                ctx.deps.trace_id,
                "output_validation_retry",
                reason="chart_required",
                retry_attempt=ctx.retry,
                max_retries=ctx.max_retries,
            )
            raise ModelRetry(
                "The user explicitly requested a chart. Do not finish yet and do not rerun "
                "SQL; call generate_chart with the verified rows, then return the narrative."
            )
        report = _data_result_to_report("", output, query_result)
        if query_result.total_rows == 0 and not _empty_result_is_disclosed(
            " ".join(
                [
                    output.direct_answer,
                    *output.highlights,
                    *output.caveats,
                ]
            )
        ):
            return _normalize_verified_output(
                ctx,
                query_result,
                reason="empty_result_not_disclosed",
            )
        evidence = assess_report_evidence(
            report,
            query_result.rows,
            query_result.sql,
            reference_date=ctx.deps.reference_date,
            prior_verified_rows=[
                row
                for prior_result in ctx.deps.prior_query_results
                for row in prior_result.rows
                if not prior_result.truncated
            ],
        )
        if not evidence.is_supported:
            return _normalize_verified_output(
                ctx,
                query_result,
                reason="unsupported_numeric_claim",
                unsupported_claim_count=len(evidence.unsupported_numeric_claims),
            )
        narrative_violation = narrative_output_violation(
            [output.direct_answer, *output.highlights],
            query_result.rows,
        )
        if narrative_violation is not None:
            return _normalize_verified_output(
                ctx,
                query_result,
                reason=narrative_violation,
            )
        if output.chart_artifact is not None and (
            ctx.deps.last_chart_artifact is None
            or output.chart_artifact != ctx.deps.last_chart_artifact
        ):
            ctx.deps.logger.event(
                ctx.deps.trace_id,
                "output_validation_normalized",
                reason="unverified_chart_artifact",
                retry_attempt=ctx.retry,
                max_retries=ctx.max_retries,
            )
            return output.model_copy(update={"chart_artifact": None})
        return output

    return analysis_agent


def _analysis_model_settings(config: AgentConfig) -> ModelSettings:
    settings: dict[str, Any] = {
        "temperature": config.model.temperature,
        "max_tokens": config.model.max_output_tokens,
    }
    if config.model.llm_model.partition(":")[0] in {"google", "google-cloud"}:
        settings["google_thinking_config"] = {
            "thinking_budget": config.model.thinking_budget,
        }
    return ModelSettings(**settings)


def _record_output_retry(
    ctx: RunContext[AgentDependencies],
    reason: str,
    **details: Any,
) -> None:
    ctx.deps.output_validation_retries = ctx.retry + 1
    ctx.deps.output_retry_reasons.append(reason)
    ctx.deps.logger.event(
        ctx.deps.trace_id,
        "output_validation_retry",
        reason=reason,
        retry_attempt=ctx.retry,
        max_retries=ctx.max_retries,
        **details,
    )


def _normalize_verified_output(
    ctx: RunContext[AgentDependencies],
    query_result: QueryResult,
    *,
    reason: str,
    **details: Any,
) -> DataAnalysisResult:
    ctx.deps.logger.event(
        ctx.deps.trace_id,
        "output_validation_normalized",
        reason=reason,
        retry_attempt=ctx.retry,
        max_retries=ctx.max_retries,
        **details,
    )
    if not query_result.rows:
        return DataAnalysisResult(
            direct_answer="No matching data was found in the verified query result.",
            caveats=["The verified warehouse query returned zero matching rows."],
        )

    summaries = [_verified_row_summary(row) for row in query_result.rows[:2]]
    return DataAnalysisResult(
        direct_answer=f"Leading verified result — {summaries[0]}.",
        highlights=[f"Next verified result — {summaries[1]}."] if len(summaries) > 1 else [],
        caveats=["Summary is limited to exact values returned by the warehouse query."],
    )


def _verified_row_summary(row: dict[str, Any]) -> str:
    return "; ".join(
        f"{column.replace('_', ' ')}: {_format_verified_value(column, value)}"
        for column, value in row.items()
    )


def _format_verified_value(column: str, value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value)
    normalized = column.casefold()
    column_words = set(re.findall(r"[a-z0-9]+", normalized))
    if column_words & {"pct", "percent", "percentage"}:
        return f"{value:.2f}".rstrip("0").rstrip(".") + "%"
    if any(token in normalized for token in ("rate", "ratio", "share")):
        return f"{value * 100:.2f}".rstrip("0").rstrip(".") + "%"
    if any(
        token in normalized
        for token in ("amount", "cost", "price", "revenue", "sales", "spend", "value")
    ):
        return f"${value:,.2f}"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.3f}".rstrip("0").rstrip(".")


def _retrieval_query(
    question: str,
    prior_query_results: tuple[QueryResult, ...],
) -> str:
    if not prior_query_results:
        return question
    return (
        f"Follow-up question: {question}\nPrior verified cohort SQL: {prior_query_results[-1].sql}"
    )


def _prior_cohort_context(prior_query_results: tuple[QueryResult, ...]) -> str:
    if not prior_query_results:
        return ""
    return (
        "Most recent verified cohort SQL from this conversation:\n"
        f"{prior_query_results[-1].sql}\n"
        "When the user refers to the same or that cohort, preserve its source timestamp, "
        "entity filters, status filters, and relative time bounds exactly unless the user "
        "explicitly changes one of them. Change only the requested measure or grouping.\n"
    )


def _retrieved_golden_context(deps: AgentDependencies) -> str:
    if not deps.retrieval_attempted:
        return ""
    if not deps.retrieved_trios:
        return (
            "Approved Golden Knowledge retrieval was attempted but unavailable. "
            "Proceed only with the verified schema and disclose degradation.\n"
        )
    examples = [
        {
            "id": example.id,
            "question": example.question,
            "sql": example.sql,
            "analyst_report": example.analyst_report,
        }
        for example in deps.retrieved_trios
    ]
    return (
        "Approved Golden Knowledge already retrieved by the application; do not call "
        "retrieve_golden_examples again. Apply relevant definitions, then run SQL:\n"
        f"{json.dumps(examples, separators=(',', ':'))}\n"
    )


async def run_question(
    question: str,
    *,
    config: AgentConfig,
    bigquery: AnalyticsGateway,
    golden_store: GoldenExampleRepository,
    chart_executor: ChartCodeExecutor | None = None,
    logger: Telemetry,
    user_id: str,
    conversation: ConversationState | None = None,
    analysis_agent: AnalysisAgentRunner | None = None,
    analysis_model: Any | None = None,
    reference_date_override: date | None = None,
) -> TurnResult:
    state = conversation or ConversationState()
    turn_index = state.turn_index + 1
    trace_id = new_trace_id()
    reference_date = reference_date_override or datetime.now(UTC).date()
    started = time.perf_counter()
    user = config.user_profile(user_id)
    history = state.message_history(
        config.conversation.max_history_turns,
        config.conversation.max_history_bytes,
    )
    retrieval_required = requires_golden_precedent(
        question,
        has_history=bool(history),
    )
    schema_only = is_schema_question(question)
    non_query_disposition = classify_non_query_request(question)
    logger.event(
        trace_id,
        "agent_run_started",
        user_id=user_id,
        question=question,
        session_id=state.session_id,
        turn_index=turn_index,
        history_messages=len(history),
        retrieval_required=retrieval_required,
        schema_only=schema_only,
        deterministic_disposition=(
            non_query_disposition.kind if non_query_disposition is not None else None
        ),
        reference_date=reference_date.isoformat(),
        model=config.model.llm_model,
        prompt_version=build_analysis_prompt().version,
        persona_version=config.persona_version,
        golden_index_version=config.retrieval.collection,
    )

    if schema_only or non_query_disposition is not None:
        if schema_only:
            response = _safe_schema_report(question, config, trace_id)
            deterministic_route = "schema"
        else:
            assert non_query_disposition is not None
            response = AnalysisReport(
                question=question,
                answer=non_query_disposition.answer,
                refused=non_query_disposition.refused,
                trace_id=trace_id,
            )
            deterministic_route = non_query_disposition.kind
        return _complete_deterministic_turn(
            response=response,
            route=deterministic_route,
            config=config,
            logger=logger,
            state=state,
            trace_id=trace_id,
            turn_index=turn_index,
            reference_date=reference_date,
            started=started,
        )

    deps = AgentDependencies(
        config=config,
        bigquery=bigquery,
        logger=logger,
        user=user,
        trace_id=trace_id,
        question=question,
        reference_date=reference_date,
        golden_store=golden_store,
        chart_executor=chart_executor,
        prior_query_results=tuple(
            result
            for result in state.completed_query_results
            if result is not None and not result.truncated
        ),
        retrieval_required=retrieval_required,
        schema_only=schema_only,
        chart_requested=bool(_CHART_REQUEST_PATTERN.search(question)),
    )

    if retrieval_required:
        await _retrieve_golden_context(
            deps,
            question=_retrieval_query(question, deps.prior_query_results),
            limit=1,
        )

    prompt = (
        f"User question: {question}\n"
        "Use the bounded conversation history to resolve follow-up references. "
        "Call only the tools needed for this question.\n"
        f"{_prior_cohort_context(deps.prior_query_results)}"
        f"{_retrieved_golden_context(deps)}"
        f"Return a report matching preferred format {user.preferred_format}."
    )
    runner = analysis_agent or build_analysis_agent(config)
    deps.model_attempt_tool_event_offset = len(deps.tool_events)
    try:
        while True:
            try:
                result = await runner.run(
                    prompt,
                    deps=deps,
                    model=analysis_model or config.model.llm_model,
                    message_history=history or None,
                    conversation_id=state.session_id,
                    model_settings=_analysis_model_settings(config),
                    usage_limits=_usage_limits(config),
                )
                break
            except UnexpectedModelBehavior as exc:
                can_recover_without_repeating_warehouse_work = (
                    deps.model_behavior_retries < 1
                    and deps.last_tool_failure_code is None
                    and (not deps.sql_tool_invoked or deps.last_query_result is not None)
                )
                if not can_recover_without_repeating_warehouse_work:
                    raise
                deps.failed_provider_requests += _minimum_current_model_requests(deps)
                deps.model_behavior_retries += 1
                logger.event(
                    trace_id,
                    "model_behavior_retry",
                    retry_attempt=deps.model_behavior_retries,
                    failure_category=_model_behavior_failure_category(exc),
                    query_already_verified=deps.last_query_result is not None,
                    warehouse_will_repeat=False,
                )
                prompt = _model_behavior_recovery_prompt(prompt, deps)
                deps.model_attempt_tool_event_offset = len(deps.tool_events)
    except Exception as exc:
        if (
            deps.chart_requested
            and deps.last_chart_artifact is None
            and deps.last_chart_failure_code is None
        ):
            deps.last_chart_failure_code = "not_generated"
        failure_code, retryable = _classify_failure(
            exc,
            tool_failure_code=deps.last_tool_failure_code,
            tool_failure_retryable=deps.last_tool_failure_retryable,
        )
        degraded = deps.last_query_result is not None
        duration_ms = _duration_ms(started)
        operational = _operational_metrics(
            deps,
            duration_ms=duration_ms,
            usage=None,
        )
        provider_failure = _provider_failure_details(
            exc,
            configured_attempts=config.model.provider_retry_attempts,
        )
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
            chart_artifact=(
                deps.last_chart_artifact.model_dump()
                if deps.last_chart_artifact is not None
                else None
            ),
            duration_ms=duration_ms,
            operational=operational.model_dump(),
            **provider_failure,
        )
        next_state = state.fail_turn(
            query_result=deps.last_query_result,
            max_turns=config.conversation.max_history_turns,
            max_bytes=config.conversation.max_history_bytes,
        )
        if deps.last_query_result is not None:
            response: AnalysisResponse = _build_degraded_report(
                question,
                deps.last_query_result,
                trace_id,
                chart_artifact=deps.last_chart_artifact,
                chart_failure_code=deps.last_chart_failure_code,
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
            chart_artifact=deps.last_chart_artifact,
            reference_date=reference_date,
            trace_id=trace_id,
            operational=operational,
        )

    response = _to_analysis_response(
        question,
        result.output,
        deps,
        trace_id,
    )
    new_messages = _new_messages(result)
    next_state = state.complete_turn(
        messages=new_messages,
        query_result=deps.last_query_result,
        max_turns=config.conversation.max_history_turns,
        max_bytes=config.conversation.max_history_bytes,
    )
    usage = _usage(result)
    duration_ms = _duration_ms(started)
    operational = _operational_metrics(
        deps,
        duration_ms=duration_ms,
        usage=usage,
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
        usage=usage,
        output_validation_retries=deps.output_validation_retries,
        chart_artifact=(
            deps.last_chart_artifact.model_dump() if deps.last_chart_artifact is not None else None
        ),
        duration_ms=duration_ms,
        operational=operational.model_dump(),
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
        chart_artifact=deps.last_chart_artifact,
        reference_date=reference_date,
        trace_id=trace_id,
        operational=operational,
    )


def _complete_deterministic_turn(
    *,
    response: AnalysisReport,
    route: str,
    config: AgentConfig,
    logger: Telemetry,
    state: ConversationState,
    trace_id: str,
    turn_index: int,
    reference_date: date,
    started: float,
) -> TurnResult:
    duration_ms = _duration_ms(started)
    operational = OperationalMetrics(
        trace_ids=[trace_id],
        duration_ms=duration_ms,
        turn_durations_ms=[duration_ms],
        provider_requests=0,
    )
    next_state = state.complete_turn(
        messages=(),
        query_result=None,
        max_turns=config.conversation.max_history_turns,
        max_bytes=config.conversation.max_history_bytes,
    )
    logger.event(
        trace_id,
        "agent_run_completed",
        session_id=state.session_id,
        turn_index=turn_index,
        refused=response.refused,
        degraded=False,
        sql=None,
        retrieved_trio_ids=[],
        retrieval_attempted=False,
        retrieval_degraded=False,
        tool_sequence=[],
        tool_events=[],
        usage=None,
        output_validation_retries=0,
        chart_artifact=None,
        duration_ms=duration_ms,
        operational=operational.model_dump(),
        prompt_version=build_analysis_prompt().version,
        persona_version=config.persona_version,
        golden_index_version=config.retrieval.collection,
        deterministic_route=route,
    )
    return TurnResult(
        response=response,
        conversation=next_state,
        reference_date=reference_date,
        trace_id=trace_id,
        operational=operational,
    )


def _safe_schema_report(
    question: str,
    config: AgentConfig,
    trace_id: str,
) -> AnalysisReport:
    lines = ["Safe analyzable schema:"]
    for table_name in config.bigquery.allowed_tables:
        columns = config.safety.safe_columns_by_table[table_name]
        rendered_columns = ", ".join(f"`{column}`" for column in columns)
        full_name = f"{config.bigquery.dataset}.{table_name}"
        lines.append(f"- `{full_name}`: {rendered_columns}")
    return AnalysisReport(
        question=question,
        answer="\n".join(lines),
        caveats=[
            "Columns not listed are unavailable to analysis, including direct "
            "identifiers and precise location fields."
        ],
        followups=[
            "Ask for an aggregate metric, comparison, trend, or chart using these fields."
        ],
        trace_id=trace_id,
    )


def _log_sql_retry_feedback(
    ctx: RunContext[AgentDependencies], *, failure_class: str, error: str
) -> None:
    ctx.deps.sql_retry_reasons.append(failure_class)
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
        report = output.model_copy(
            update={
                "sql": (deps.last_query_result.sql if deps.last_query_result is not None else None),
                "chart_artifact": deps.last_chart_artifact,
            }
        )
    elif isinstance(output, DataAnalysisResult):
        if deps.last_query_result is None:
            return AgentFailure(
                question=question,
                message=_failure_message("retry_exhausted"),
                failure_code="retry_exhausted",
                trace_id=trace_id,
                retryable=False,
            )
        report = _data_result_to_report(
            question,
            output,
            deps.last_query_result,
            chart_artifact=deps.last_chart_artifact,
        )
    elif isinstance(output, SchemaExplanationResult):
        report = AnalysisReport(
            question=question,
            answer=output.explanation,
            caveats=output.caveats,
            followups=output.followups,
        )
    elif isinstance(output, ClarificationRequest):
        report = AnalysisReport(question=question, answer=output.question)
    elif isinstance(output, UnsupportedRequest):
        report = AnalysisReport(
            question=question,
            answer=output.reason,
            refused=True,
        )
    elif isinstance(output, ExecutionFailure):
        failure_code: FailureCode = "model_unavailable" if output.retryable else "internal_error"
        return AgentFailure(
            question=question,
            message=_failure_message(failure_code),
            failure_code=failure_code,
            trace_id=trace_id,
            retryable=output.retryable,
        )
    else:
        raise TypeError(f"Unsupported analysis output: {type(output)!r}")
    return _finalize_report(report, deps, trace_id)


def _data_result_to_report(
    question: str,
    output: DataAnalysisResult,
    query_result: QueryResult,
    *,
    chart_artifact: ChartArtifact | None = None,
) -> AnalysisReport:
    if query_result.truncated:
        return _truncated_query_report(question, query_result)
    return AnalysisReport(
        question=question,
        answer=output.direct_answer,
        highlights=output.highlights,
        table=query_result.rows,
        total_rows=query_result.total_rows,
        available_rows=query_result.available_rows,
        truncated=query_result.truncated,
        row_limit=query_result.row_limit,
        sql=query_result.sql,
        caveats=output.caveats,
        followups=output.followups,
        chart_artifact=chart_artifact,
    )


def _finalize_report(
    report: AnalysisReport,
    deps: AgentDependencies,
    trace_id: str,
) -> AnalysisReport:
    caveats = _with_chart_failure_caveat(
        report.caveats,
        deps.last_chart_failure_code,
    )
    if deps.retrieval_degraded:
        warning = (
            "Approved Golden Knowledge was unavailable; this report used only the "
            "verified warehouse result."
        )
        if warning not in caveats:
            caveats = [*caveats, warning]
    report = report.model_copy(
        update={
            "caveats": caveats,
            "degraded": report.degraded or deps.retrieval_degraded,
        }
    )
    return _sanitize_report(report, trace_id)


def _with_chart_failure_caveat(
    caveats: list[str],
    failure_code: ChartFailureCode | None,
) -> list[str]:
    if failure_code is None:
        return caveats
    warning = (
        f"Chart generation was unavailable ({failure_code}); "
        "the verified analysis is still available."
    )
    return caveats if warning in caveats else [*caveats, warning]


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


def _record_dependency_tool_event(
    deps: AgentDependencies,
    tool_name: str,
    status: str,
    started: float,
) -> None:
    event = {
        "tool": tool_name,
        "status": status,
        "duration_ms": _duration_ms(started),
    }
    deps.tool_events.append(event)
    deps.logger.event(deps.trace_id, "agent_tool_completed", **event)


def _sanitize_report(report: AnalysisReport, trace_id: str) -> AnalysisReport:
    data: dict[str, Any] = report.model_dump()
    redacted, _ = redact_value(data)
    redacted["trace_id"] = trace_id
    return AnalysisReport.model_validate(redacted)


def _build_degraded_report(
    question: str,
    query_result: QueryResult,
    trace_id: str,
    *,
    chart_artifact: ChartArtifact | None = None,
    chart_failure_code: ChartFailureCode | None = None,
) -> AnalysisReport:
    if query_result.truncated:
        report = _truncated_query_report(question, query_result).model_copy(
            update={"degraded": True}
        )
        return _sanitize_report(report, trace_id)
    return _sanitize_report(
        AnalysisReport(
            question=question,
            answer=(
                "The narrative model became unavailable after the data query completed. "
                "The verified query results are shown below."
            ),
            table=query_result.rows,
            total_rows=query_result.total_rows,
            available_rows=query_result.available_rows,
            truncated=query_result.truncated,
            row_limit=query_result.row_limit,
            sql=query_result.sql,
            caveats=_with_chart_failure_caveat(
                ["Narrative interpretation is unavailable; retry for a full report."],
                chart_failure_code,
            ),
            degraded=True,
            chart_artifact=chart_artifact,
        ),
        trace_id,
    )


def _truncated_query_report(
    question: str,
    query_result: QueryResult,
) -> AnalysisReport:
    available_rows = query_result.available_rows or query_result.total_rows
    preview_size = min(20, query_result.total_rows)
    return AnalysisReport(
        question=question,
        answer=(
            f"The query produced {available_rows} rows, which exceeds the "
            f"{query_result.row_limit or query_result.total_rows}-row analysis limit. "
            "Narrow the date range or dimensions before drawing conclusions or a chart."
        ),
        table=query_result.rows[:preview_size],
        total_rows=query_result.total_rows,
        available_rows=available_rows,
        truncated=True,
        row_limit=query_result.row_limit,
        sql=query_result.sql,
        caveats=[f"Showing a {preview_size}-row preview only; the result is incomplete."],
        followups=["Which date range or dimension should I narrow first?"],
    )


def _provider_failure_details(
    exc: Exception,
    *,
    configured_attempts: int,
) -> dict[str, Any]:
    provider_exc = _nested_provider_exception(exc)
    status_code = provider_exc.status_code if isinstance(provider_exc, ModelHTTPError) else None
    if status_code == 429:
        category = "rate_limited"
    elif status_code in {408, 500, 502, 503, 504}:
        category = "transient_provider_error"
    elif isinstance(provider_exc, (ModelAPIError, ConnectionError, TimeoutError)):
        category = "provider_unavailable"
    else:
        category = "non_provider_error"
    retry_count = (
        max(0, configured_attempts - 1) if status_code in {408, 429, 500, 502, 503, 504} else 0
    )
    return {
        "provider_status": (
            f"http_{status_code}"
            if status_code is not None
            else (
                "transport_error"
                if isinstance(provider_exc, (ModelAPIError, ConnectionError, TimeoutError))
                else "not_applicable"
            )
        ),
        "provider_status_code": status_code,
        "provider_retry_count": retry_count,
        "provider_terminal_category": category,
        "provider_error_category": category,
    }


def _model_behavior_failure_category(exc: UnexpectedModelBehavior) -> str:
    normalized = " ".join(str(exc).casefold().split())
    if "output" in normalized and "retr" in normalized:
        return "output_retry_exhausted"
    if "tool" in normalized and "retr" in normalized:
        return "tool_retry_exhausted"
    if "validation" in normalized:
        return "structured_output_invalid"
    return "unexpected_model_behavior"


def _model_behavior_recovery_prompt(prompt: str, deps: AgentDependencies) -> str:
    context: list[str] = [
        "Recovery instruction: the prior model response did not satisfy the tool or "
        "structured-output contract. Continue once without repeating any successful "
        "warehouse query."
    ]
    if deps.retrieved_trios:
        context.append(_retrieved_golden_context(deps))
    if deps.last_query_result is not None:
        rows, _ = redact_value(deps.last_query_result.rows[:2])
        columns = sorted(
            {column for row in deps.last_query_result.rows for column in row}
        )
        context.append(
            "The warehouse query already succeeded and run_sql_query is now hidden. "
            f"Available columns: {columns}. "
            f"Verified sample rows: {json.dumps(rows, default=str, sort_keys=True)}. "
            "Use the existing verified result for the final narrative and any requested chart."
        )
    return f"{prompt}\n\n" + "\n".join(part for part in context if part)


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
    if isinstance(
        _nested_provider_exception(exc),
        (ModelAPIError, ConnectionError, TimeoutError),
    ):
        return "model_unavailable", True
    return "internal_error", False


def _nested_provider_exception(exc: Exception) -> Exception:
    if isinstance(exc, (ModelAPIError, ConnectionError, TimeoutError)):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in reversed(exc.exceptions):
            if isinstance(nested, Exception):
                provider_exc = _nested_provider_exception(nested)
                if isinstance(
                    provider_exc,
                    (ModelAPIError, ConnectionError, TimeoutError),
                ):
                    return provider_exc
    return exc


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


def _operational_metrics(
    deps: AgentDependencies,
    *,
    duration_ms: int,
    usage: Any,
) -> OperationalMetrics:
    retrieval_events = [
        event for event in deps.tool_events if event["tool"] == "retrieve_golden_examples"
    ]
    query_events = [event for event in deps.tool_events if event["tool"] == "run_sql_query"]
    chart_events = [event for event in deps.tool_events if event["tool"] == "generate_chart"]
    input_tokens = getattr(usage, "input_tokens", None)
    cached_input_tokens = getattr(usage, "cache_read_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    details = getattr(usage, "details", {}) or {}
    reasoning_tokens = details.get("reasoning_tokens")
    total_tokens = getattr(usage, "total_tokens", None)
    if callable(total_tokens):
        total_tokens = total_tokens()
    provider_requests = deps.failed_provider_requests + (
        getattr(usage, "requests", 1)
        if usage is not None
        else _minimum_current_model_requests(deps)
    )
    return OperationalMetrics(
        trace_ids=[deps.trace_id],
        duration_ms=duration_ms,
        turn_durations_ms=[duration_ms],
        provider_requests=provider_requests,
        retrieval_requests=len(retrieval_events),
        query_attempts=len(query_events),
        sql_retries=max(0, len(query_events) - 1),
        sql_retry_reasons=deps.sql_retry_reasons,
        output_retries=deps.output_validation_retries,
        output_retry_reasons=deps.output_retry_reasons,
        bigquery_dry_runs=deps.bigquery_dry_runs,
        bigquery_executions=deps.bigquery_executions,
        bigquery_job_ids=deps.bigquery_job_ids,
        duplicate_warehouse_executions=max(0, deps.bigquery_executions - 1),
        tool_sequence=deps.tool_sequence,
        tool_order_compliant=_tool_order_is_compliant(deps.tool_events),
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        reasoning_tokens=reasoning_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        dry_run_bytes=deps.dry_run_bytes,
        billed_bytes=deps.billed_bytes,
        cache_hit=any(deps.cache_hits) if deps.cache_hits else None,
        chart_duration_ms=(
            sum(event["duration_ms"] for event in chart_events) if chart_events else None
        ),
        chart_artifact_bytes=(
            deps.last_chart_artifact.size_bytes if deps.last_chart_artifact is not None else None
        ),
    )


def _minimum_current_model_requests(deps: AgentDependencies) -> int:
    model_tool_events = deps.tool_events[deps.model_attempt_tool_event_offset :]
    return 1 + sum(
        event["tool"]
        in {"retrieve_golden_examples", "run_sql_query", "generate_chart"}
        for event in model_tool_events
    )


def _tool_order_is_compliant(tool_events: list[dict[str, Any]]) -> bool:
    sql_events = [
        index for index, event in enumerate(tool_events) if event["tool"] == "run_sql_query"
    ]
    successful_sql = [
        index
        for index, event in enumerate(tool_events)
        if event["tool"] == "run_sql_query" and event["status"] == "succeeded"
    ]
    if len(successful_sql) > 1:
        return False
    if sql_events and any(
        index > sql_events[0]
        for index, event in enumerate(tool_events)
        if event["tool"] == "retrieve_golden_examples"
    ):
        return False
    chart_events = [
        index for index, event in enumerate(tool_events) if event["tool"] == "generate_chart"
    ]
    if chart_events and (not successful_sql or min(chart_events) < successful_sql[0]):
        return False
    return True


def _empty_result_is_disclosed(narrative: str) -> bool:
    normalized = " ".join(narrative.casefold().split())
    return any(
        phrase in normalized
        for phrase in (
            "no matching data",
            "no matching rows",
            "no matching records",
            "no matching results",
            "no data was found",
            "no rows were found",
            "did not return any data",
            "did not return any rows",
        )
    )


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
