from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import ModelRetry, UsageLimits
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelHTTPError,
    ToolRetryError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from retail_agent import agent
from retail_agent.bigquery import (
    QueryCostExceeded,
    QueryOutcomeUnknownError,
    QueryPreExecutionError,
)
from retail_agent.domain.errors import ChartExecutionError
from retail_agent.infrastructure.charts import LocalPythonChartExecutor
from retail_agent.models import (
    AgentFailure,
    AnalysisReport,
    ChartArtifact,
    DataAnalysisResult,
    ExecutionFailure,
    QueryResult,
    RetrievedTrio,
    SchemaExplanationResult,
)
from retail_agent.observability import EventLogger


class FakeGoldenStore:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []
        self.results = [
            RetrievedTrio(
                id="trio_monthly_revenue_category",
                score=0.91,
                question="Which categories drove revenue?",
                sql="SELECT category, SUM(revenue) AS revenue FROM table GROUP BY category",
                analyst_report="Rank categories by revenue and include order count.",
                tags=["revenue", "category"],
            )
        ]

    def search(self, question: str, trace_id: str, limit: int = 3):
        self.calls.append({"question": question, "trace_id": trace_id, "limit": limit})
        return self.results


class FailingGoldenStore:
    def search(self, question: str, trace_id: str, limit: int = 3):
        raise RuntimeError("Qdrant collection is unavailable")


class FailingChartExecutor:
    async def execute(self, request):
        raise ChartExecutionError(
            "chart process failed",
            code="process_failed",
            repair_hint="Fix line 4, use order_count, and save chart.png.",
        )


class CapturingChartExecutor:
    def __init__(self):
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return ChartArtifact(
            path="artifacts/charts/chart.png",
            output_format=request.output_format,
            size_bytes=100,
            code_digest="a" * 64,
        )


class FakeBigQueryRunner:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def describe_allowed_tables(self) -> str:
        return "- `bigquery-public-data.thelook_ecommerce.order_items`: id INTEGER"

    def execute(self, sql: str, trace_id: str) -> QueryResult:
        self.calls.append((sql, trace_id))
        return QueryResult(
            sql=sql,
            rows=[{"order_count": 42}],
            total_rows=1,
            dry_run_bytes=1024,
            total_bytes_billed=1024,
            job_id=f"job-{trace_id}",
            cache_hit=False,
        )


@dataclass
class FakeRunResult:
    output: Any
    messages: list[ModelMessage]

    def new_messages(self) -> list[ModelMessage]:
        return self.messages

    def usage(self) -> RunUsage:
        return RunUsage(
            requests=1,
            input_tokens=100,
            cache_read_tokens=20,
            output_tokens=30,
            details={"reasoning_tokens": 5},
        )


class FakeAnalysisAgent:
    def __init__(self, callback):
        self.callback = callback
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        prompt: str,
        *,
        deps,
        model,
        message_history=None,
        conversation_id=None,
        model_settings=None,
        usage_limits=None,
    ):
        kwargs = {
            "deps": deps,
            "model": model,
            "message_history": message_history,
            "conversation_id": conversation_id,
            "model_settings": model_settings,
            "usage_limits": usage_limits,
        }
        self.calls.append({"prompt": prompt, **kwargs})
        result = self.callback(prompt, **kwargs)
        return await result if inspect.isawaitable(result) else result


def _message(content: str) -> ModelMessage:
    return ModelRequest(parts=[UserPromptPart(content=content)])


def _events(path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _agent_dependencies(
    test_config,
    tmp_path,
    *,
    golden_store=None,
    chart_executor=None,
):
    return agent.AgentDependencies(
        config=test_config,
        bigquery=FakeBigQueryRunner(),
        logger=EventLogger(tmp_path / "native-agent.jsonl"),
        user=test_config.user_profile("manager_a"),
        trace_id="native-agent-trace",
        golden_store=golden_store,
        chart_executor=chart_executor,
    )


def test_run_question_defers_golden_retrieval_to_the_model(test_config, tmp_path):
    golden_store = FakeGoldenStore()

    def callback(prompt, *, deps, **kwargs):
        deps.last_query_result = QueryResult(
            sql="SELECT category, revenue FROM safe_table LIMIT 10",
            rows=[{"category": "Outerwear", "revenue": 100}],
            total_rows=1,
        )
        deps.sql_tool_invoked = True
        return FakeRunResult(
            output=AnalysisReport(
                question="Which categories drove revenue?",
                answer="Outerwear led revenue.",
                sql="SELECT 1",
            ),
            messages=[_message("first turn")],
        )

    fake_agent = FakeAnalysisAgent(callback)
    log_path = tmp_path / "runs.jsonl"
    turn = asyncio.run(
        agent.run_question(
            "Which categories drove revenue?",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=golden_store,
            logger=EventLogger(log_path),
            user_id="manager_a",
            analysis_agent=fake_agent,
        )
    )

    assert isinstance(turn.response, AnalysisReport)
    assert turn.response.answer == "Outerwear led revenue."
    assert turn.response.sql == "SELECT category, revenue FROM safe_table LIMIT 10"
    assert turn.sql_tool_invoked is True
    assert turn.conversation.turn_index == 1
    assert len(turn.conversation.completed_turns) == 1
    assert turn.retrieved_trio_ids == ()
    assert golden_store.calls == []
    assert "Golden Knowledge analyst precedents" not in fake_agent.calls[0]["prompt"]
    assert "Call only the tools needed" in fake_agent.calls[0]["prompt"]
    started = [event for event in _events(log_path) if event["event"] == "agent_run_started"]
    assert started[0]["prompt_version"] == "analysis-v11"
    assert started[0]["reference_date"] == turn.reference_date.isoformat()
    assert started[0]["persona_version"] == "prototype-config-v1"
    assert started[0]["golden_index_version"] == "test_trios"


def test_schema_question_is_deterministic_safe_and_skips_model_and_warehouse(
    test_config, tmp_path
):
    class TrackingBigQuery(FakeBigQueryRunner):
        def __init__(self):
            super().__init__()
            self.schema_calls = 0

        def describe_allowed_tables(self) -> str:
            self.schema_calls += 1
            return "unsafe physical schema"

    bigquery = TrackingBigQuery()
    fake_agent = FakeAnalysisAgent(lambda *args, **kwargs: pytest.fail("model was called"))

    turn = asyncio.run(
        agent.run_question(
            "What safe retail tables and columns can you analyze?",
            config=test_config,
            bigquery=bigquery,
            golden_store=FakeGoldenStore(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            user_id="manager_a",
            analysis_agent=fake_agent,
        )
    )

    assert isinstance(turn.response, AnalysisReport)
    assert "safe analyzable schema" in turn.response.answer.lower()
    assert "order_items" in turn.response.answer
    assert "sale_price" in turn.response.answer
    assert "postal_code" not in turn.response.answer
    assert "email" not in turn.response.answer
    assert bigquery.schema_calls == 0
    assert bigquery.calls == []
    assert fake_agent.calls == []
    assert turn.operational.provider_requests == 0
    assert turn.operational.tool_sequence == []
    assert turn.conversation.turn_index == 1


def test_available_tables_caveat_does_not_turn_analysis_into_schema_request(
    test_config, tmp_path
):
    fake_agent = FakeAnalysisAgent(
        lambda *args, **kwargs: FakeRunResult(
            output=AnalysisReport(
                question="question",
                answer="Analysis route used.",
            ),
            messages=[],
        )
    )

    turn = asyncio.run(
        agent.run_question(
            "Show return patterns; the available tables cannot establish why.",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=FakeGoldenStore(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            user_id="manager_a",
            analysis_agent=fake_agent,
        )
    )

    assert fake_agent.calls
    assert turn.operational.provider_requests == 1


def test_verified_value_formatter_respects_percentage_point_columns() -> None:
    assert agent._format_verified_value("growth_rate_pct", 2661.43) == "2661.43%"
    assert agent._format_verified_value("return_rate", 0.1268) == "12.68%"


@pytest.mark.parametrize(
    ("question", "refused", "answer_fragment"),
    [
        (
            "Who are our best customers right now?",
            False,
            "what should define a best customer",
        ),
        (
            "Show all customers, but only return the top ten.",
            False,
            "choose one scope",
        ),
        (
            "What is our visitor-to-order conversion rate by traffic source?",
            True,
            "no visit, session, or impression denominator",
        ),
        (
            "Compare physical store branches and identify the weakest location.",
            False,
            "no physical store-branch dimension",
        ),
    ],
)
def test_high_confidence_non_query_routes_skip_model_and_warehouse(
    test_config, tmp_path, question, refused, answer_fragment
):
    bigquery = FakeBigQueryRunner()
    golden_store = FakeGoldenStore()
    fake_agent = FakeAnalysisAgent(lambda *args, **kwargs: pytest.fail("model was called"))

    turn = asyncio.run(
        agent.run_question(
            question,
            config=test_config,
            bigquery=bigquery,
            golden_store=golden_store,
            logger=EventLogger(tmp_path / "runs.jsonl"),
            user_id="manager_a",
            analysis_agent=fake_agent,
        )
    )

    assert isinstance(turn.response, AnalysisReport)
    assert turn.response.refused is refused
    assert answer_fragment in turn.response.answer.lower()
    assert bigquery.calls == []
    assert golden_store.calls == []
    assert fake_agent.calls == []
    assert turn.operational.provider_requests == 0
    assert turn.operational.query_attempts == 0
    assert turn.operational.tool_sequence == []
    assert turn.conversation.turn_index == 1


def test_run_question_prefetches_required_golden_context_once(test_config, tmp_path):
    golden_store = FakeGoldenStore()
    bigquery = FakeBigQueryRunner()

    async def callback(prompt, *, deps, model_settings, **kwargs):
        assert deps.retrieval_attempted is True
        assert deps.retrieved_trio_ids == ["trio_monthly_revenue_category"]
        assert deps.tool_sequence == ["retrieve_golden_examples"]
        assert "Approved Golden Knowledge already retrieved" in prompt
        assert golden_store.results[0].sql in prompt
        assert model_settings["temperature"] == 0
        assert model_settings["google_thinking_config"] == {"thinking_budget": 0}
        await agent.run_sql_query(
            SimpleNamespace(deps=deps, retry=0, max_retries=2),
            "SELECT 42 AS order_count",
        )
        return FakeRunResult(
            output=AnalysisReport(
                question="Which products sell well but have high return risk?",
                answer="There were 42 matching items.",
            ),
            messages=[_message("required retrieval")],
        )

    turn = asyncio.run(
        agent.run_question(
            "Which products sell well but have high return risk?",
            config=test_config,
            bigquery=bigquery,
            golden_store=golden_store,
            logger=EventLogger(tmp_path / "required-retrieval.jsonl"),
            user_id="manager_a",
            analysis_agent=FakeAnalysisAgent(callback),
        )
    )

    assert len(golden_store.calls) == 1
    assert golden_store.calls[0]["limit"] == 1
    assert turn.retrieved_trio_ids == ("trio_monthly_revenue_category",)
    assert turn.operational.retrieval_requests == 1
    assert turn.operational.tool_sequence == [
        "retrieve_golden_examples",
        "run_sql_query",
    ]


def test_retrieval_tool_is_hidden_after_first_attempt(test_config, tmp_path):
    deps = _agent_dependencies(test_config, tmp_path, golden_store=FakeGoldenStore())
    deps.retrieval_attempted = True

    prepared = asyncio.run(
        agent.prepare_retrieval_tool(
            SimpleNamespace(deps=deps),
            SimpleNamespace(name="retrieve_golden_examples"),
        )
    )

    assert prepared is None


def test_run_question_returns_attributed_operational_metrics(test_config, tmp_path):
    bigquery = FakeBigQueryRunner()

    async def callback(prompt, *, deps, **kwargs):
        await agent.run_sql_query(
            SimpleNamespace(deps=deps, retry=0, max_retries=2),
            "SELECT 42 AS order_count",
        )
        return FakeRunResult(
            output=AnalysisReport(question="How many orders?", answer="There were 42 orders."),
            messages=[_message("orders")],
        )

    turn = asyncio.run(
        agent.run_question(
            "How many orders?",
            config=test_config,
            bigquery=bigquery,
            golden_store=FakeGoldenStore(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            user_id="manager_a",
            analysis_agent=FakeAnalysisAgent(callback),
        )
    )

    assert turn.trace_id is not None
    assert turn.operational.trace_ids == [turn.trace_id]
    assert turn.operational.provider_requests == 1
    assert turn.operational.query_attempts == 1
    assert turn.operational.bigquery_dry_runs == 1
    assert turn.operational.bigquery_executions == 1
    assert turn.operational.bigquery_job_ids == [f"job-{turn.trace_id}"]
    assert turn.operational.input_tokens == 100
    assert turn.operational.cached_input_tokens == 20
    assert turn.operational.reasoning_tokens == 5
    assert turn.operational.output_tokens == 30
    assert turn.operational.total_tokens == 130
    assert turn.operational.dry_run_bytes == 1024
    assert turn.operational.billed_bytes == 1024
    assert turn.operational.tool_order_compliant is True


def test_run_question_continues_when_golden_knowledge_is_unavailable(test_config, tmp_path):
    async def callback(prompt, *, deps, **kwargs):
        result = await agent.retrieve_golden_examples(
            SimpleNamespace(deps=deps),
            "How many orders?",
        )
        assert result.status == "degraded"
        assert result.error_code == "retrieval_unavailable"
        deps.last_query_result = QueryResult(
            sql="SELECT COUNT(*) AS order_count FROM safe_table LIMIT 10",
            rows=[{"order_count": 42}],
            total_rows=1,
        )
        return FakeRunResult(
            output=AnalysisReport(question="How many orders?", answer="There were 42 orders."),
            messages=[_message("orders")],
        )

    log_path = tmp_path / "runs.jsonl"
    turn = asyncio.run(
        agent.run_question(
            "How many orders?",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=FailingGoldenStore(),
            logger=EventLogger(log_path),
            user_id="manager_a",
            analysis_agent=FakeAnalysisAgent(callback),
        )
    )

    assert isinstance(turn.response, AnalysisReport)
    assert turn.response.answer == "There were 42 orders."
    assert turn.response.degraded is True
    assert turn.response.caveats == [
        "Approved Golden Knowledge was unavailable; this report used only the "
        "verified warehouse result."
    ]
    unavailable = [
        event for event in _events(log_path) if event["event"] == "golden_knowledge_unavailable"
    ]
    assert unavailable[0]["failure_class"] == "RuntimeError"


def test_run_question_passes_history_and_contextualizes_follow_up(test_config, tmp_path):
    golden_store = FakeGoldenStore()

    first_agent = FakeAnalysisAgent(
        lambda prompt, **kwargs: FakeRunResult(
            output=AnalysisReport(question="Revenue last month?", answer="Revenue was 100."),
            messages=[_message("first turn message")],
        )
    )
    first = asyncio.run(
        agent.run_question(
            "Revenue last month?",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=golden_store,
            logger=EventLogger(tmp_path / "runs.jsonl"),
            user_id="manager_a",
            analysis_agent=first_agent,
        )
    )

    def second_callback(prompt, *, message_history, conversation_id, **kwargs):
        assert len(message_history) == 1
        assert message_history[0].parts[0].content == "first turn message"
        assert conversation_id == first.conversation.session_id
        assert "Previous question:" not in prompt
        assert "bounded conversation history" in prompt
        return FakeRunResult(
            output=AnalysisReport(question="Compare that with prior month", answer="It increased."),
            messages=[_message("second turn message")],
        )

    second = asyncio.run(
        agent.run_question(
            "Compare that with the prior month",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=golden_store,
            logger=EventLogger(tmp_path / "runs.jsonl"),
            user_id="manager_a",
            conversation=first.conversation,
            analysis_agent=FakeAnalysisAgent(second_callback),
        )
    )

    assert isinstance(second.response, AnalysisReport)
    assert second.response.answer == "It increased."
    assert second.conversation.turn_index == 2
    assert second.conversation.session_id == first.conversation.session_id
    assert len(golden_store.calls) == 2
    assert golden_store.calls[0]["question"] == "Revenue last month?"
    assert golden_store.calls[1]["question"] == "Compare that with the prior month"


def test_model_failure_returns_typed_failure_and_logs_event(test_config, tmp_path):
    def callback(prompt, **kwargs):
        raise ConnectionError("Gemini is down: secret-provider-detail")

    log_path = tmp_path / "runs.jsonl"
    turn = asyncio.run(
        agent.run_question(
            "How many orders?",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=FakeGoldenStore(),
            logger=EventLogger(log_path),
            user_id="manager_a",
            analysis_agent=FakeAnalysisAgent(callback),
        )
    )

    assert isinstance(turn.response, AgentFailure)
    assert turn.response.failure_code == "model_unavailable"
    assert turn.response.retryable is True
    assert "secret-provider-detail" not in turn.response.message
    assert turn.conversation.turn_index == 1
    failed = [event for event in _events(log_path) if event["event"] == "agent_run_failed"]
    assert failed[0]["failure_code"] == "model_unavailable"
    assert failed[0]["degraded"] is False


def test_unexpected_model_behavior_recovers_once_before_query(test_config, tmp_path):
    calls = 0

    def callback(prompt, *, deps, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise UnexpectedModelBehavior("Exceeded maximum output retries (1)")
        deps.last_query_result = QueryResult(
            sql="SELECT 42 AS orders",
            rows=[{"orders": 42}],
            total_rows=1,
        )
        deps.sql_tool_invoked = True
        return FakeRunResult(
            output=DataAnalysisResult(direct_answer="There were 42 orders."),
            messages=[_message("recovered")],
        )

    log_path = tmp_path / "runs.jsonl"
    turn = asyncio.run(
        agent.run_question(
            "How many orders?",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=FakeGoldenStore(),
            logger=EventLogger(log_path),
            user_id="manager_a",
            analysis_agent=FakeAnalysisAgent(callback),
        )
    )

    assert isinstance(turn.response, AnalysisReport)
    assert turn.response.answer == "There were 42 orders."
    assert calls == 2
    assert turn.operational.provider_requests == 2
    retries = [event for event in _events(log_path) if event["event"] == "model_behavior_retry"]
    assert retries[0]["failure_category"] == "output_retry_exhausted"
    assert retries[0]["warehouse_will_repeat"] is False


def test_unexpected_model_behavior_recovers_after_query_without_reexecution(
    test_config, tmp_path
):
    warehouse = FakeBigQueryRunner()
    calls = 0

    def callback(prompt, *, deps, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            result = warehouse.execute("SELECT 42 AS orders", deps.trace_id)
            deps.last_query_result = result
            deps.sql_tool_invoked = True
            deps.bigquery_dry_runs = 1
            deps.bigquery_executions = 1
            deps.bigquery_job_ids.append(result.job_id)
            deps.tool_sequence.append("run_sql_query")
            deps.tool_events.append(
                {"tool": "run_sql_query", "status": "succeeded", "duration_ms": 1}
            )
            raise UnexpectedModelBehavior("Structured output validation failed")
        assert "warehouse query already succeeded" in prompt
        assert "Available columns: ['order_count']" in prompt
        return FakeRunResult(
            output=DataAnalysisResult(direct_answer="There were 42 orders."),
            messages=[_message("recovered")],
        )

    turn = asyncio.run(
        agent.run_question(
            "How many orders?",
            config=test_config,
            bigquery=warehouse,
            golden_store=FakeGoldenStore(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            user_id="manager_a",
            analysis_agent=FakeAnalysisAgent(callback),
        )
    )

    assert isinstance(turn.response, AnalysisReport)
    assert len(warehouse.calls) == 1
    assert turn.operational.query_attempts == 1
    assert turn.operational.bigquery_executions == 1
    assert turn.operational.duplicate_warehouse_executions == 0
    assert turn.operational.provider_requests == 3


def test_structured_execution_failure_is_user_safe(test_config, tmp_path):
    fake_agent = FakeAnalysisAgent(
        lambda prompt, **kwargs: FakeRunResult(
            output=ExecutionFailure(
                message="provider-secret-detail",
                retryable=True,
            ),
            messages=[_message("failed turn")],
        )
    )

    turn = asyncio.run(
        agent.run_question(
            "How many orders?",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=FakeGoldenStore(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            user_id="manager_a",
            analysis_agent=fake_agent,
        )
    )

    assert isinstance(turn.response, AgentFailure)
    assert turn.response.failure_code == "model_unavailable"
    assert turn.response.retryable is True
    assert "provider-secret-detail" not in turn.response.message


def test_unknown_warehouse_outcome_is_non_retryable_and_user_safe(test_config, tmp_path):
    def callback(prompt, **kwargs):
        raise QueryOutcomeUnknownError(
            "provider detail must stay hidden",
            job_id="retail_agent_trace_deadbeef",
        )

    log_path = tmp_path / "runs.jsonl"
    turn = asyncio.run(
        agent.run_question(
            "How many orders?",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=FakeGoldenStore(),
            logger=EventLogger(log_path),
            user_id="manager_a",
            analysis_agent=FakeAnalysisAgent(callback),
        )
    )

    assert isinstance(turn.response, AgentFailure)
    assert turn.response.failure_code == "warehouse_outcome_unknown"
    assert turn.response.retryable is False
    assert "provider detail" not in turn.response.message
    assert "Do not retry immediately" in turn.response.message


def test_failure_after_query_returns_redacted_degraded_report(test_config, tmp_path):
    def callback(prompt, *, deps, **kwargs):
        deps.last_query_result = QueryResult(
            sql="SELECT 42 AS orders LIMIT 1",
            rows=[{"orders": 42, "contact": "private@example.com"}],
            total_rows=1,
        )
        deps.sql_tool_invoked = True
        raise ConnectionError("Gemini disconnected")

    turn = asyncio.run(
        agent.run_question(
            "How many orders?",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=FakeGoldenStore(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            user_id="manager_a",
            analysis_agent=FakeAnalysisAgent(callback),
        )
    )

    assert isinstance(turn.response, AnalysisReport)
    assert turn.response.degraded is True
    assert turn.response.table[0]["contact"] == "[REDACTED_EMAIL]"
    assert turn.response.sql == "SELECT 42 AS orders LIMIT 1"
    assert turn.sql_tool_invoked is True


def test_provider_failure_logs_normalized_retry_metadata_without_body(test_config, tmp_path):
    log_path = tmp_path / "runs.jsonl"

    def callback(prompt, **kwargs):
        raise ModelHTTPError(
            429,
            "gemini-2.5-flash",
            body={"secret": "raw-provider-body"},
        )

    turn = asyncio.run(
        agent.run_question(
            "How many orders?",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=FakeGoldenStore(),
            logger=EventLogger(log_path),
            user_id="manager_a",
            analysis_agent=FakeAnalysisAgent(callback),
        )
    )

    failed = [event for event in _events(log_path) if event["event"] == "agent_run_failed"]
    assert isinstance(turn.response, AgentFailure)
    assert failed[0]["provider_status"] == "http_429"
    assert failed[0]["provider_retry_count"] == 2
    assert failed[0]["provider_terminal_category"] == "rate_limited"
    assert "raw-provider-body" not in log_path.read_text(encoding="utf-8")


def test_provider_failure_unwraps_endpoint_fallback_group(test_config, tmp_path):
    log_path = tmp_path / "runs.jsonl"

    def callback(prompt, **kwargs):
        raise FallbackExceptionGroup(
            "Both Vertex endpoints failed",
            [
                ModelHTTPError(429, "gemini-2.5-flash", body={"endpoint": "global"}),
                ModelHTTPError(
                    503,
                    "gemini-2.5-flash",
                    body={"endpoint": "us-central1"},
                ),
            ],
        )

    turn = asyncio.run(
        agent.run_question(
            "How many orders?",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=FakeGoldenStore(),
            logger=EventLogger(log_path),
            user_id="manager_a",
            analysis_agent=FakeAnalysisAgent(callback),
        )
    )

    failed = [event for event in _events(log_path) if event["event"] == "agent_run_failed"]
    assert isinstance(turn.response, AgentFailure)
    assert turn.response.failure_code == "model_unavailable"
    assert turn.response.retryable is True
    assert failed[0]["provider_status"] == "http_503"
    assert failed[0]["provider_terminal_category"] == "transient_provider_error"
    assert "endpoint" not in log_path.read_text(encoding="utf-8")


def test_exhausted_warehouse_tool_failure_keeps_retryable_classification(test_config, tmp_path):
    def callback(prompt, *, deps, **kwargs):
        deps.last_tool_failure_code = "warehouse_unavailable"
        deps.last_tool_failure_retryable = True
        raise ToolRetryError(RetryPromptPart("BigQuery unavailable"))

    turn = asyncio.run(
        agent.run_question(
            "How many orders?",
            config=test_config,
            bigquery=FakeBigQueryRunner(),
            golden_store=FakeGoldenStore(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            user_id="manager_a",
            analysis_agent=FakeAnalysisAgent(callback),
        )
    )

    assert isinstance(turn.response, AgentFailure)
    assert turn.response.failure_code == "warehouse_unavailable"
    assert turn.response.retryable is True


@pytest.mark.parametrize(
    ("effect", "failure_code", "retryable"),
    [
        (QueryCostExceeded("over budget"), "retry_exhausted", False),
        (QueryPreExecutionError("unavailable"), "warehouse_unavailable", True),
        (ValueError("invalid SQL"), "retry_exhausted", False),
    ],
)
def test_sql_tool_records_failure_before_model_retry(
    test_config, tmp_path, effect, failure_code, retryable
):
    class StubWarehouse(FakeBigQueryRunner):
        def execute(self, sql: str, trace_id: str) -> QueryResult:
            if isinstance(effect, Exception):
                raise effect
            return effect

    deps = agent.AgentDependencies(
        config=test_config,
        bigquery=StubWarehouse(),
        logger=EventLogger(tmp_path / "runs.jsonl"),
        user=test_config.user_profile("manager_a"),
        trace_id="trace",
    )
    context = SimpleNamespace(deps=deps, retry=0, max_retries=2)

    with pytest.raises(ModelRetry):
        asyncio.run(agent.run_sql_query(context, "SELECT 1"))

    assert deps.last_tool_failure_code == failure_code
    assert deps.last_tool_failure_retryable is retryable
    assert deps.sql_tool_invoked is True


def test_sql_tool_rejects_unrequested_realized_breakdown_before_warehouse(
    test_config, tmp_path
):
    warehouse = FakeBigQueryRunner()
    deps = agent.AgentDependencies(
        config=test_config,
        bigquery=warehouse,
        logger=EventLogger(tmp_path / "runs.jsonl"),
        user=test_config.user_profile("manager_a"),
        trace_id="trace",
        question=(
            "Report realized sales while consistently excluding cancelled and "
            "returned items."
        ),
    )
    context = SimpleNamespace(deps=deps, retry=0, max_retries=2)

    with pytest.raises(ModelRetry, match="one realized-sales total"):
        asyncio.run(
            agent.run_sql_query(
                context,
                """
                SELECT DATE_TRUNC(DATE(created_at), MONTH) AS sales_month,
                       SUM(sale_price) AS realized_sales
                FROM `bigquery-public-data.thelook_ecommerce.order_items`
                WHERE status NOT IN ('Cancelled', 'Returned')
                GROUP BY sales_month
                """,
            )
        )

    assert warehouse.calls == []
    assert deps.bigquery_dry_runs == 0
    assert deps.bigquery_executions == 0


def test_sql_tool_preserves_valid_empty_result_without_retry(test_config, tmp_path):
    empty = QueryResult(
        sql="SELECT product_id FROM safe_table WHERE FALSE",
        rows=[],
        total_rows=0,
        dry_run_bytes=128,
        total_bytes_billed=0,
    )

    class EmptyWarehouse(FakeBigQueryRunner):
        def execute(self, sql: str, trace_id: str) -> QueryResult:
            return empty

    deps = agent.AgentDependencies(
        config=test_config,
        bigquery=EmptyWarehouse(),
        logger=EventLogger(tmp_path / "runs.jsonl"),
        user=test_config.user_profile("manager_a"),
        trace_id="trace",
    )

    result = asyncio.run(
        agent.run_sql_query(SimpleNamespace(deps=deps, retry=0, max_retries=2), empty.sql)
    )

    assert result.returned_rows == 0
    assert result.preview_rows == []
    assert result.truncated is False
    assert deps.last_query_result is empty
    assert deps.last_tool_failure_code is None
    assert deps.bigquery_executions == 1
    assert deps.bigquery_job_ids == []
    assert agent._empty_result_is_disclosed("No matching data was found.") is True
    assert agent._empty_result_is_disclosed("The analysis completed.") is False


def test_sql_tool_returns_bounded_model_preview_and_retains_full_result(
    test_config, tmp_path
):
    complete = QueryResult(
        sql="SELECT value FROM safe_table ORDER BY value",
        rows=[{"value": index} for index in range(25)],
        total_rows=25,
        available_rows=25,
        truncated=False,
        row_limit=500,
    )

    class CompleteWarehouse(FakeBigQueryRunner):
        def execute(self, sql: str, trace_id: str) -> QueryResult:
            return complete

    deps = agent.AgentDependencies(
        config=test_config,
        bigquery=CompleteWarehouse(),
        logger=EventLogger(tmp_path / "runs.jsonl"),
        user=test_config.user_profile("manager_a"),
        trace_id="trace",
    )

    result = asyncio.run(
        agent.run_sql_query(
            SimpleNamespace(deps=deps, retry=0, max_retries=2), complete.sql
        )
    )

    assert result.columns == ["value"]
    assert result.preview_rows == [{"value": index} for index in range(10)]
    assert result.preview_row_count == 10
    assert result.returned_rows == 25
    assert result.available_rows == 25
    assert result.truncated is False
    assert deps.last_query_result is complete
    assert len(deps.last_query_result.rows) == 25


def test_tool_order_rejects_retrieval_after_sql():
    events = [
        {"tool": "retrieve_golden_examples", "status": "succeeded"},
        {"tool": "run_sql_query", "status": "succeeded"},
        {"tool": "retrieve_golden_examples", "status": "succeeded"},
    ]

    assert agent._tool_order_is_compliant(events) is False


def test_sql_tool_does_not_model_retry_unknown_query_outcome(test_config, tmp_path):
    class OutcomeUnknownWarehouse(FakeBigQueryRunner):
        def execute(self, sql: str, trace_id: str) -> QueryResult:
            raise QueryOutcomeUnknownError("outcome unknown", job_id="stable-job")

    log_path = tmp_path / "runs.jsonl"
    deps = agent.AgentDependencies(
        config=test_config,
        bigquery=OutcomeUnknownWarehouse(),
        logger=EventLogger(log_path),
        user=test_config.user_profile("manager_a"),
        trace_id="trace",
    )
    context = SimpleNamespace(deps=deps, retry=0, max_retries=2)

    with pytest.raises(QueryOutcomeUnknownError):
        asyncio.run(agent.run_sql_query(context, "SELECT 1"))

    assert deps.last_tool_failure_code == "warehouse_outcome_unknown"
    assert deps.last_tool_failure_retryable is False
    events = _events(log_path)
    assert not any(event["event"] == "sql_retry_feedback" for event in events)
    terminal = [event for event in events if event["event"] == "sql_terminal_failure"]
    assert terminal[0]["job_id"] == "stable-job"


def test_sql_tool_clears_prior_failure_after_success(test_config, tmp_path):
    deps = agent.AgentDependencies(
        config=test_config,
        bigquery=FakeBigQueryRunner(),
        logger=EventLogger(tmp_path / "runs.jsonl"),
        user=test_config.user_profile("manager_a"),
        trace_id="trace",
        last_tool_failure_code="retry_exhausted",
    )
    context = SimpleNamespace(deps=deps, retry=1, max_retries=2)

    result = asyncio.run(agent.run_sql_query(context, "SELECT 1"))

    assert result.returned_rows == 1
    assert deps.last_query_result is not None
    assert deps.last_query_result.total_rows == 1
    assert deps.last_tool_failure_code is None
    assert deps.sql_tool_invoked is True


def test_runtime_context_includes_identity_preferences_and_schema(test_config, tmp_path):
    deps = agent.AgentDependencies(
        config=test_config,
        bigquery=FakeBigQueryRunner(),
        logger=EventLogger(tmp_path / "runs.jsonl"),
        user=test_config.user_profile("manager_a"),
        trace_id="trace",
    )

    context = asyncio.run(agent.add_runtime_context(SimpleNamespace(deps=deps)))

    assert "Trace ID: trace" in context
    assert "Current UTC date:" in context
    assert context.count("Current UTC date:") == 1
    assert "Allowed BigQuery tables" in context
    assert "Preferred report format" in context


def test_sql_tool_waits_for_required_retrieval_attempt(test_config, tmp_path):
    deps = _agent_dependencies(test_config, tmp_path)
    deps.retrieval_required = True
    definition = SimpleNamespace(name="run_sql_query")

    hidden = asyncio.run(agent.prepare_sql_tool(SimpleNamespace(deps=deps), definition))
    deps.retrieval_attempted = True
    visible = asyncio.run(agent.prepare_sql_tool(SimpleNamespace(deps=deps), definition))
    deps.last_query_result = QueryResult(
        sql="SELECT 1",
        rows=[{"value": 1}],
        total_rows=1,
    )
    hidden_after_success = asyncio.run(
        agent.prepare_sql_tool(SimpleNamespace(deps=deps), definition)
    )

    assert hidden is None
    assert visible is definition
    assert hidden_after_success is None


def test_schema_only_question_hides_every_execution_tool(test_config, tmp_path):
    deps = _agent_dependencies(test_config, tmp_path, chart_executor=CapturingChartExecutor())
    deps.schema_only = True
    deps.last_query_result = QueryResult(
        sql="SELECT 42 AS order_count",
        rows=[{"order_count": 42}],
        total_rows=1,
    )
    context = SimpleNamespace(deps=deps)

    assert asyncio.run(agent.prepare_retrieval_tool(context, SimpleNamespace())) is None
    assert asyncio.run(agent.prepare_sql_tool(context, SimpleNamespace())) is None
    assert asyncio.run(agent.prepare_chart_tool(context, SimpleNamespace())) is None


@pytest.mark.parametrize("retry_budget", [0, 1, 2, 3])
def test_build_analysis_agent_applies_configured_tool_retry_budget(test_config, retry_budget):
    configured = test_config.model_copy(
        update={
            "agent_limits": test_config.agent_limits.model_copy(
                update={"max_sql_retries": retry_budget}
            )
        }
    )

    toolset = agent.build_analysis_toolset(configured)

    assert set(toolset.tools) == {
        "generate_chart",
        "retrieve_golden_examples",
        "run_sql_query",
    }
    assert toolset.tools["retrieve_golden_examples"].max_retries == 0
    assert toolset.tools["run_sql_query"].max_retries == retry_budget
    assert toolset.tools["generate_chart"].max_retries == test_config.agent_limits.max_chart_retries


def test_native_agent_can_query_without_retrieval(test_config, tmp_path):
    deps = _agent_dependencies(test_config, tmp_path, golden_store=FakeGoldenStore())
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("run_sql_query", {"sql": "SELECT 42 AS order_count"})]
            )
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
        )
        return ModelResponse(
            parts=[ToolCallPart(output_tool.name, {"direct_answer": "There were 42 orders."})]
        )

    model = FunctionModel(model_function)

    result = asyncio.run(
        agent.build_analysis_agent(test_config).run(
            "How many orders?",
            deps=deps,
            model=model,
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert result.output.direct_answer == "There were 42 orders."
    assert deps.tool_sequence == ["run_sql_query"]
    assert deps.retrieval_attempted is False


def test_native_agent_continues_after_retrieval_degrades(test_config, tmp_path):
    deps = _agent_dependencies(test_config, tmp_path, golden_store=FailingGoldenStore())
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "retrieve_golden_examples",
                        {"question": "How many orders?"},
                    )
                ]
            )
        if calls == 2:
            return ModelResponse(
                parts=[ToolCallPart("run_sql_query", {"sql": "SELECT 42 AS order_count"})]
            )
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
        )
        return ModelResponse(
            parts=[ToolCallPart(output_tool.name, {"direct_answer": "There were 42 orders."})]
        )

    model = FunctionModel(model_function)

    result = asyncio.run(
        agent.build_analysis_agent(test_config).run(
            "How many orders?",
            deps=deps,
            model=model,
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert deps.tool_sequence == ["retrieve_golden_examples", "run_sql_query"]
    assert deps.retrieval_degraded is True
    assert deps.last_query_result is not None


def test_required_retrieval_unlocks_sql_without_spending_retry_budget(test_config, tmp_path):
    deps = _agent_dependencies(test_config, tmp_path, golden_store=FakeGoldenStore())
    deps.retrieval_required = True
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        tools = {tool.name for tool in info.function_tools}
        if calls == 1:
            assert "retrieve_golden_examples" in tools
            assert "run_sql_query" not in tools
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "retrieve_golden_examples",
                        {"question": "top customers by spend"},
                    )
                ]
            )
        if calls == 2:
            assert "run_sql_query" in tools
            return ModelResponse(
                parts=[ToolCallPart("run_sql_query", {"sql": "SELECT 42 AS order_count"})]
            )
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
        )
        return ModelResponse(
            parts=[ToolCallPart(output_tool.name, {"direct_answer": "There were 42 orders."})]
        )

    result = asyncio.run(
        agent.build_analysis_agent(test_config).run(
            "Who are our top customers by spend?",
            deps=deps,
            model=FunctionModel(model_function),
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert deps.tool_sequence == ["retrieve_golden_examples", "run_sql_query"]
    assert deps.output_validation_retries == 0


def test_output_validator_normalizes_unsupported_claim_without_provider_retry(
    test_config, tmp_path
):
    deps = _agent_dependencies(test_config, tmp_path)
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "run_sql_query",
                        {"sql": "SELECT 42 AS order_count"},
                    )
                ]
            )
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
        )
        return ModelResponse(
            parts=[ToolCallPart(output_tool.name, {"direct_answer": "There were 99 orders."})]
        )

    result = asyncio.run(
        agent.build_analysis_agent(test_config).run(
            "How many orders?",
            deps=deps,
            model=FunctionModel(model_function),
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert "42" in result.output.direct_answer
    assert "99" not in result.output.direct_answer
    assert deps.output_validation_retries == 0
    assert deps.output_retry_reasons == []
    assert len(deps.bigquery.calls) == 1
    assert deps.bigquery_job_ids == ["job-native-agent-trace"]
    assert calls == 2


def test_output_validator_normalizes_final_unsupported_claim(test_config, tmp_path):
    config = test_config.model_copy(
        update={"agent_limits": test_config.agent_limits.model_copy(update={"output_retries": 1})}
    )
    deps = _agent_dependencies(config, tmp_path)
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("run_sql_query", {"sql": "SELECT 42 AS order_count"})]
            )
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
        )
        return ModelResponse(
            parts=[ToolCallPart(output_tool.name, {"direct_answer": "There were 99 orders."})]
        )

    result = asyncio.run(
        agent.build_analysis_agent(config).run(
            "How many orders?",
            deps=deps,
            model=FunctionModel(model_function),
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert "42" in result.output.direct_answer
    assert "99" not in result.output.direct_answer
    assert deps.output_validation_retries == 0
    assert deps.output_retry_reasons == []
    assert len(deps.bigquery.calls) == 1
    assert calls == 2


def test_output_validator_requires_empty_result_disclosure_without_rerunning_sql(
    test_config, tmp_path
):
    class EmptyBigQueryRunner(FakeBigQueryRunner):
        def execute(self, sql: str, trace_id: str) -> QueryResult:
            self.calls.append((sql, trace_id))
            return QueryResult(
                sql=sql,
                rows=[],
                total_rows=0,
                dry_run_bytes=1024,
                total_bytes_billed=1024,
                job_id=f"job-{trace_id}",
                cache_hit=False,
            )

    deps = _agent_dependencies(test_config, tmp_path)
    deps.bigquery = EmptyBigQueryRunner()
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("run_sql_query", {"sql": "SELECT 1 WHERE FALSE"})]
            )
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
        )
        return ModelResponse(
            parts=[ToolCallPart(output_tool.name, {"direct_answer": "Analysis completed."})]
        )

    result = asyncio.run(
        agent.build_analysis_agent(test_config).run(
            "Are there matching orders?",
            deps=deps,
            model=FunctionModel(model_function),
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert result.output.direct_answer == "No matching data was found in the verified query result."
    assert deps.output_retry_reasons == []
    assert len(deps.bigquery.calls) == 1
    assert calls == 2


def test_output_validator_normalizes_markdown_table_without_provider_retry(
    test_config, tmp_path
):
    deps = _agent_dependencies(test_config, tmp_path)
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "run_sql_query",
                        {"sql": "SELECT 42 AS order_count"},
                    )
                ]
            )
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
        )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {"direct_answer": "| Metric | Value |\n| --- | ---: |\n| Orders | 42 |"},
                )
            ]
        )

    result = asyncio.run(
        agent.build_analysis_agent(test_config).run(
            "How many orders?",
            deps=deps,
            model=FunctionModel(model_function),
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert "42" in result.output.direct_answer
    assert "|" not in result.output.direct_answer
    assert deps.output_validation_retries == 0
    assert calls == 2


def test_usage_limit_rejects_tool_batch_before_execution(test_config, tmp_path):
    deps = _agent_dependencies(test_config, tmp_path, golden_store=FakeGoldenStore())
    model = TestModel(
        call_tools=["retrieve_golden_examples", "run_sql_query"],
        custom_output_args={"direct_answer": "There were 42 orders."},
    )

    with pytest.raises(UsageLimitExceeded):
        asyncio.run(
            agent.build_analysis_agent(test_config).run(
                "How many orders?",
                deps=deps,
                model=model,
                usage_limits=UsageLimits(tool_calls_limit=1),
            )
        )

    assert deps.tool_sequence == []


def test_history_processor_keeps_multiple_complete_recent_turns(test_config, tmp_path):
    configured = test_config.model_copy(
        update={
            "conversation": test_config.conversation.model_copy(update={"max_history_turns": 2})
        }
    )
    deps = _agent_dependencies(configured, tmp_path)
    seen_messages: list[ModelMessage] = []

    def model_function(messages, info):
        seen_messages.extend(messages)
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("SchemaExplanationResult")
        )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {"explanation": "The dataset contains retail orders."},
                )
            ]
        )

    history = [
        ModelRequest(parts=[UserPromptPart(content="oldest question")]),
        ModelResponse(parts=[TextPart(content="oldest answer")]),
        ModelRequest(parts=[UserPromptPart(content="middle question")]),
        ModelResponse(parts=[TextPart(content="middle answer")]),
        ModelRequest(parts=[UserPromptPart(content="latest question")]),
        ModelResponse(parts=[TextPart(content="latest answer")]),
    ]
    result = asyncio.run(
        agent.build_analysis_agent(configured).run(
            "Explain the schema",
            deps=deps,
            model=FunctionModel(model_function),
            message_history=history,
        )
    )

    serialized = ModelMessagesTypeAdapter.dump_json(seen_messages).decode()
    assert isinstance(result.output, SchemaExplanationResult)
    assert "oldest question" not in serialized
    assert "middle question" in serialized
    assert "latest question" in serialized
    assert "Explain the schema" in serialized


def test_chart_tool_becomes_available_only_after_verified_query(test_config, tmp_path):
    configured = test_config.model_copy(
        update={
            "chart_execution": test_config.chart_execution.model_copy(
                update={"artifact_directory": tmp_path / "charts"}
            )
        }
    )
    chart_executor = LocalPythonChartExecutor(configured.chart_execution)
    deps = _agent_dependencies(
        configured,
        tmp_path,
        chart_executor=chart_executor,
    )
    deps.last_chart_failure_code = "timeout"
    visible_tools: list[set[str]] = []
    calls = 0
    chart_code = """
import json
from pathlib import Path
rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
value = rows[0]["order_count"]
Path("chart.svg").write_text(
    f'<svg xmlns="http://www.w3.org/2000/svg"><text>{value}</text></svg>',
    encoding="utf-8",
)
"""

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        tool_names = {tool.name for tool in info.function_tools}
        visible_tools.append(tool_names)
        if calls == 1:
            assert "generate_chart" not in tool_names
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "run_sql_query",
                        {"sql": "SELECT 42 AS order_count"},
                    )
                ]
            )
        if calls == 2:
            assert "generate_chart" in tool_names
            assert "run_sql_query" not in tool_names
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "generate_chart",
                        {"code": chart_code, "output_format": "svg"},
                    )
                ]
            )
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
        )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {"direct_answer": "There were 42 orders."},
                )
            ]
        )

    result = asyncio.run(
        agent.build_analysis_agent(configured).run(
            "Plot the order count",
            deps=deps,
            model=FunctionModel(model_function),
        )
    )
    response = agent._to_analysis_response(
        "Plot the order count",
        result.output,
        deps,
        "trace",
    )

    assert isinstance(response, AnalysisReport)
    assert response.chart_artifact is not None
    assert deps.last_chart_failure_code is None
    assert Path(response.chart_artifact.path).is_file()
    assert deps.tool_sequence == ["run_sql_query", "generate_chart"]
    assert "generate_chart" not in visible_tools[0]
    assert "generate_chart" in visible_tools[1]


def test_chart_request_cannot_finish_before_generate_chart(test_config, tmp_path):
    deps = _agent_dependencies(
        test_config,
        tmp_path,
        chart_executor=CapturingChartExecutor(),
    )
    deps.chart_requested = True
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("run_sql_query", {"sql": "SELECT 42 AS order_count"})]
            )
        if calls == 2:
            output_tool = next(
                tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
            )
            return ModelResponse(
                parts=[ToolCallPart(output_tool.name, {"direct_answer": "There were 42 orders."})]
            )
        if calls == 3:
            tool_names = {tool.name for tool in info.function_tools}
            assert "generate_chart" in tool_names
            assert "run_sql_query" not in tool_names
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "generate_chart",
                        {"code": "print('chart')", "output_format": "png"},
                    )
                ]
            )
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
        )
        return ModelResponse(
            parts=[ToolCallPart(output_tool.name, {"direct_answer": "There were 42 orders."})]
        )

    result = asyncio.run(
        agent.build_analysis_agent(test_config).run(
            "Plot order count.",
            deps=deps,
            model=FunctionModel(model_function),
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert deps.last_chart_artifact is not None
    assert deps.output_retry_reasons == ["chart_required"]
    assert deps.tool_sequence == ["run_sql_query", "generate_chart"]


def test_chart_and_structured_output_can_complete_in_same_model_response(test_config, tmp_path):
    deps = _agent_dependencies(
        test_config,
        tmp_path,
        chart_executor=CapturingChartExecutor(),
    )
    deps.chart_requested = True
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("run_sql_query", {"sql": "SELECT 42 AS order_count"})]
            )
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
        )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "generate_chart",
                    {"code": "print('chart')", "output_format": "png"},
                ),
                ToolCallPart(
                    output_tool.name,
                    {"direct_answer": "There were 42 orders."},
                ),
            ]
        )

    result = asyncio.run(
        agent.build_analysis_agent(test_config).run(
            "Plot order count.",
            deps=deps,
            model=FunctionModel(model_function),
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert deps.last_chart_artifact is not None
    assert deps.tool_sequence == ["run_sql_query", "generate_chart"]
    assert calls == 2


def test_output_validator_bounds_redundant_highlights_without_retry(test_config, tmp_path):
    class ThreeRowBigQueryRunner(FakeBigQueryRunner):
        def execute(self, sql: str, trace_id: str) -> QueryResult:
            self.calls.append((sql, trace_id))
            return QueryResult(
                sql=sql,
                rows=[
                    {"product": "Boots", "revenue": 120},
                    {"product": "Jeans", "revenue": 100},
                    {"product": "Socks", "revenue": 80},
                ],
                total_rows=3,
            )

    deps = _agent_dependencies(test_config, tmp_path)
    deps.bigquery = ThreeRowBigQueryRunner()
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("run_sql_query", {"sql": "SELECT product, revenue"})]
            )
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
        )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    output_tool.name,
                    {
                        "direct_answer": "Boots led the verified results.",
                        "highlights": [
                            "Boots generated 120.",
                            "Jeans generated 100.",
                            "Socks generated 80.",
                        ],
                    },
                )
            ]
        )

    result = asyncio.run(
        agent.build_analysis_agent(test_config).run(
            "Which product led?",
            deps=deps,
            model=FunctionModel(model_function),
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert result.output.highlights == ["Boots generated 120.", "Jeans generated 100."]
    assert deps.output_validation_retries == 0
    assert calls == 2


def test_chart_failure_is_added_to_final_report(test_config, tmp_path):
    deps = _agent_dependencies(
        test_config,
        tmp_path,
        chart_executor=FailingChartExecutor(),
    )
    deps.last_query_result = QueryResult(
        sql="SELECT 42 AS order_count",
        rows=[{"order_count": 42}],
        total_rows=1,
    )

    tool_result = asyncio.run(
        agent.generate_chart(
            SimpleNamespace(deps=deps),
            "raise RuntimeError('boom')",
        )
    )
    response = agent._to_analysis_response(
        "Plot the order count",
        DataAnalysisResult(direct_answer="There were 42 orders."),
        deps,
        "trace",
    )

    assert tool_result.status == "error"
    assert tool_result.error_code == "process_failed"
    assert deps.last_chart_artifact is None
    assert deps.last_chart_failure_code == "process_failed"
    assert isinstance(response, AnalysisReport)
    assert response.chart_artifact is None
    assert response.caveats == [
        "Chart generation was unavailable (process_failed); "
        "the verified analysis is still available."
    ]


def test_chart_failure_requests_two_repairs_before_returning_error(test_config, tmp_path):
    deps = _agent_dependencies(
        test_config,
        tmp_path,
        chart_executor=FailingChartExecutor(),
    )
    deps.last_query_result = QueryResult(
        sql="SELECT 42 AS order_count",
        rows=[{"order_count": 42}],
        total_rows=1,
    )

    for retry in (0, 1):
        with pytest.raises(ModelRetry, match="Fix line 4"):
            asyncio.run(
                agent.generate_chart(
                    SimpleNamespace(deps=deps, retry=retry, max_retries=2),
                    "raise RuntimeError('boom')",
                )
            )

    final = asyncio.run(
        agent.generate_chart(
            SimpleNamespace(deps=deps, retry=2, max_retries=2),
            "raise RuntimeError('boom')",
        )
    )

    assert deps.chart_attempts == 3
    assert final.status == "error"
    assert final.repair_hint == "Fix line 4, use order_count, and save chart.png."
    assert (
        asyncio.run(agent.prepare_chart_tool(SimpleNamespace(deps=deps), SimpleNamespace())) is None
    )


def test_chart_input_is_recursively_redacted_before_execution(test_config, tmp_path):
    executor = CapturingChartExecutor()
    deps = _agent_dependencies(test_config, tmp_path, chart_executor=executor)
    deps.last_query_result = QueryResult(
        sql="SELECT 42 AS revenue",
        rows=[
            {
                "revenue": 42,
                "contact": {
                    "email": "private@example.com",
                    "notes": ["call +1 415-555-0123"],
                },
            }
        ],
        total_rows=1,
    )

    result = asyncio.run(
        agent.generate_chart(
            SimpleNamespace(deps=deps, retry=0, max_retries=2),
            "print('unused')",
        )
    )

    assert result.status == "ok"
    assert executor.requests[0].data[0]["contact"] == {
        "email": "[REDACTED_EMAIL]",
        "notes": ["call [REDACTED_PHONE]"],
    }


def test_truncated_query_replaces_model_claims_with_deterministic_preview(test_config, tmp_path):
    rows = [{"category": f"Category {index}", "revenue": index} for index in range(500)]
    query_result = QueryResult(
        sql="SELECT category, revenue FROM safe_table",
        rows=rows,
        total_rows=500,
        available_rows=650,
        truncated=True,
        row_limit=500,
    )

    report = agent._data_result_to_report(
        "Plot every category",
        DataAnalysisResult(
            direct_answer="All 650 categories are completely represented.",
            chart_artifact=ChartArtifact(
                path="invented.png",
                output_format="png",
                size_bytes=100,
                code_digest="b" * 64,
            ),
        ),
        query_result,
    )

    assert report.answer.startswith("The query produced 650 rows")
    assert report.total_rows == 500
    assert report.available_rows == 650
    assert report.truncated is True
    assert report.row_limit == 500
    assert len(report.table) == 20
    assert report.chart_artifact is None
    assert "completely represented" not in report.answer


def test_output_validator_rejects_unverified_chart_reference(test_config, tmp_path):
    deps = _agent_dependencies(test_config, tmp_path)
    calls = 0

    def model_function(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[ToolCallPart("run_sql_query", {"sql": "SELECT 42 AS order_count"})]
            )
        output_tool = next(
            tool for tool in info.output_tools if tool.name.endswith("DataAnalysisResult")
        )
        output = {"direct_answer": "There were 42 orders."}
        if calls == 2:
            output["chart_artifact"] = ChartArtifact(
                path="unverified.png",
                output_format="png",
                size_bytes=100,
                code_digest="0" * 64,
            ).model_dump()
        return ModelResponse(parts=[ToolCallPart(output_tool.name, output)])

    result = asyncio.run(
        agent.build_analysis_agent(test_config).run(
            "How many orders?",
            deps=deps,
            model=FunctionModel(model_function),
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert result.output.chart_artifact is None
    assert deps.output_validation_retries == 0
    assert calls == 2


def test_conversation_state_trims_completed_turns():
    state = agent.ConversationState()
    for index in range(4):
        state = state.complete_turn(
            messages=[_message(f"m{index}")],
            max_turns=2,
        )

    assert state.turn_index == 4
    history = state.message_history(2)
    assert [message.parts[0].content for message in history] == ["m2", "m3"]


def test_conversation_state_retains_bounded_verified_query_results():
    state = agent.ConversationState()
    for index in range(3):
        state = state.complete_turn(
            messages=[_message(f"m{index}")],
            query_result=QueryResult(
                sql=f"SELECT {index} AS value",
                rows=[{"value": index}],
                total_rows=1,
            ),
            max_turns=2,
        )

    assert [result.rows for result in state.completed_query_results if result] == [
        [{"value": 1}],
        [{"value": 2}],
    ]


def test_conversation_state_compacts_large_tool_results():
    message = ModelRequest(
        parts=[
            UserPromptPart(content="question"),
            ToolReturnPart(
                tool_name="run_sql_query",
                tool_call_id="call-1",
                content={"rows": [{"description": "x" * 10_000}]},
            ),
        ]
    )

    state = agent.ConversationState().complete_turn(
        messages=[message],
        max_turns=6,
        max_bytes=2_048,
    )

    stored_request = state.completed_turns[0][0]
    assert isinstance(stored_request, ModelRequest)
    tool_return = stored_request.parts[1]
    assert isinstance(tool_return, ToolReturnPart)
    assert "omitted" in tool_return.content["summary"]
    assert state.message_history(6, max_bytes=2_048) == [stored_request]
