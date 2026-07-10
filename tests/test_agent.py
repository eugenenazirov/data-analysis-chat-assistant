from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.exceptions import ToolRetryError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    RetryPromptPart,
    ToolReturnPart,
    UserPromptPart,
)

from retail_agent import agent
from retail_agent.bigquery import (
    QueryCostExceeded,
    QueryOutcomeUnknownError,
    QueryPreExecutionError,
)
from retail_agent.models import AgentFailure, AnalysisReport, QueryResult, RetrievedTrio
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
    output: AnalysisReport
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
    ):
        kwargs = {
            "deps": deps,
            "model": model,
            "message_history": message_history,
            "conversation_id": conversation_id,
        }
        self.calls.append({"prompt": prompt, **kwargs})
        return self.callback(prompt, **kwargs)


def _message(content: str) -> ModelMessage:
    return ModelRequest(parts=[UserPromptPart(content=content)])


def _events(path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_run_question_prefetches_golden_knowledge_before_model(test_config, tmp_path):
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
    assert turn.retrieved_trio_ids == ("trio_monthly_revenue_category",)
    assert golden_store.calls[0]["limit"] == 3
    assert "Golden Knowledge analyst precedents" in fake_agent.calls[0]["prompt"]
    assert "trio_monthly_revenue_category" in fake_agent.calls[0]["prompt"]

    prepared = [
        event
        for event in _events(log_path)
        if event["event"] == "agent_golden_context_prepared"
    ]
    assert prepared[0]["ids"] == ["trio_monthly_revenue_category"]
    assert prepared[0]["session_id"] == turn.conversation.session_id
    started = [event for event in _events(log_path) if event["event"] == "agent_run_started"]
    assert started[0]["prompt_version"] == "analysis-v2"
    assert started[0]["persona_version"] == "prototype-config-v1"
    assert started[0]["golden_index_version"] == "test_trios"


def test_run_question_continues_when_golden_knowledge_is_unavailable(test_config, tmp_path):
    def callback(prompt, *, deps, **kwargs):
        assert "none retrieved" in prompt
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
        event
        for event in _events(log_path)
        if event["event"] == "golden_knowledge_unavailable"
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
        assert "Previous question: Revenue last month?" in prompt
        assert "preserve its entity, timestamp column, filters, and time bounds" in prompt
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
    assert "Previous question: Revenue last month?" in golden_store.calls[-1]["question"]
    assert "Follow-up: Compare that with the prior month" in golden_store.calls[-1]["question"]


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


def test_exhausted_warehouse_tool_failure_keeps_retryable_classification(
    test_config, tmp_path
):
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
    assert "Allowed BigQuery tables" in context
    assert "Preferred report format" in context


@pytest.mark.parametrize("retry_budget", [0, 1, 2, 3])
def test_build_analysis_agent_applies_configured_tool_retry_budget(test_config, retry_budget):
    configured = test_config.model_copy(
        update={"model": test_config.model.model_copy(update={"max_sql_retries": retry_budget})}
    )

    built = agent.build_analysis_agent(configured)

    assert set(built._function_toolset.tools) == {"run_sql_query"}
    assert built._function_toolset.tools["run_sql_query"].max_retries == retry_budget


def test_conversation_state_trims_completed_turns():
    state = agent.ConversationState()
    for index in range(4):
        state = state.complete_turn(
            question=f"q{index}",
            messages=[_message(f"m{index}")],
            max_turns=2,
        )

    assert state.turn_index == 4
    assert state.recent_questions == ("q2", "q3")
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
        question="question",
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
