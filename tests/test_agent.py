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
from pydantic_ai.exceptions import ToolRetryError, UsageLimitExceeded
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

from retail_agent import agent
from retail_agent.bigquery import (
    QueryCostExceeded,
    QueryOutcomeUnknownError,
    QueryPreExecutionError,
)
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


class FakeBigQueryRunner:
    def describe_allowed_tables(self) -> str:
        return "- `bigquery-public-data.thelook_ecommerce.order_items`: id INTEGER"

    def execute(self, sql: str, trace_id: str) -> QueryResult:
        return QueryResult(sql=sql, rows=[{"order_count": 42}], total_rows=1)


@dataclass
class FakeRunResult:
    output: Any
    messages: list[ModelMessage]

    def new_messages(self) -> list[ModelMessage]:
        return self.messages

    def usage(self) -> dict[str, int]:
        return {"requests": 1}


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
    assert started[0]["prompt_version"] == "analysis-v3"
    assert started[0]["reference_date"] == turn.reference_date.isoformat()
    assert started[0]["persona_version"] == "prototype-config-v1"
    assert started[0]["golden_index_version"] == "test_trios"


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
    assert golden_store.calls == []


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
        (QueryResult(sql="SELECT 1", rows=[], total_rows=0), "retry_exhausted", False),
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

    assert result.total_rows == 1
    assert deps.last_query_result is result
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

    assert hidden is None
    assert visible is definition


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
    assert toolset.tools["generate_chart"].max_retries == 0


def test_native_agent_can_query_without_retrieval(test_config, tmp_path):
    deps = _agent_dependencies(test_config, tmp_path, golden_store=FakeGoldenStore())
    model = TestModel(
        call_tools=["run_sql_query"],
        custom_output_args={"direct_answer": "There were 42 orders."},
    )

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
    model = TestModel(
        call_tools=["retrieve_golden_examples", "run_sql_query"],
        custom_output_args={"direct_answer": "There were 42 orders."},
    )

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


def test_required_retrieval_unlocks_sql_without_spending_retry_budget(
    test_config, tmp_path
):
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
            parts=[
                ToolCallPart(output_tool.name, {"direct_answer": "There were 42 orders."})
            ]
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


def test_output_validator_retries_unsupported_claim_then_accepts_evidence(test_config, tmp_path):
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
        answer = "There were 99 orders." if calls == 2 else "There were 42 orders."
        return ModelResponse(parts=[ToolCallPart(output_tool.name, {"direct_answer": answer})])

    result = asyncio.run(
        agent.build_analysis_agent(test_config).run(
            "How many orders?",
            deps=deps,
            model=FunctionModel(model_function),
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert result.output.direct_answer == "There were 42 orders."
    assert deps.output_validation_retries == 1
    assert calls == 3


def test_output_validator_retries_markdown_table_then_accepts_summary(test_config, tmp_path):
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
        answer = (
            "| Metric | Value |\n| --- | ---: |\n| Orders | 42 |"
            if calls == 2
            else "There were 42 orders."
        )
        return ModelResponse(parts=[ToolCallPart(output_tool.name, {"direct_answer": answer})])

    result = asyncio.run(
        agent.build_analysis_agent(test_config).run(
            "How many orders?",
            deps=deps,
            model=FunctionModel(model_function),
        )
    )

    assert isinstance(result.output, DataAnalysisResult)
    assert result.output.direct_answer == "There were 42 orders."
    assert deps.output_validation_retries == 1
    assert calls == 3


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
    assert Path(response.chart_artifact.path).is_file()
    assert deps.tool_sequence == ["run_sql_query", "generate_chart"]
    assert "generate_chart" not in visible_tools[0]
    assert "generate_chart" in visible_tools[1]


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
    assert deps.output_validation_retries == 1


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
