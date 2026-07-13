from __future__ import annotations

from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from retail_agent.application.dto import AnalyzeQuestionResponse
from retail_agent.bootstrap import RuntimeOperationError
from retail_agent.domain.models import AgentFailure, QueryResult
from retail_agent.presentation.cli import app as cli
from retail_agent.presentation.cli.app import BIGQUERY_SMOKE_SQL
from retail_agent.sql_guard import validate_and_prepare_sql


def _failing_runtime(test_config, tmp_path):
    class Runtime:
        def __init__(self):
            self.calls = 0
            self.start_conversation = SimpleNamespace(execute=self.start)

        async def start(self):
            return "conversation"

        async def analyze(self, question, *, user_id, conversation_id=None):
            self.calls += 1
            return AnalyzeQuestionResponse(
                response=AgentFailure(
                    question=question,
                    message="The analysis model is temporarily unavailable.",
                    failure_code="model_unavailable",
                    retryable=True,
                    trace_id="trace",
                ),
                conversation_id=conversation_id or "conversation",
            )

    return Runtime()


def test_bigquery_smoke_sql_is_guardrail_safe(test_config):
    validation = validate_and_prepare_sql(BIGQUERY_SMOKE_SQL, test_config)

    assert validation.tables == ["order_items"]
    assert "email" not in validation.safe_sql.lower()
    assert "phone" not in validation.safe_sql.lower()


def test_ask_model_failure_exits_cleanly_without_traceback(test_config, tmp_path, monkeypatch):
    runtime = _failing_runtime(test_config, tmp_path)
    monkeypatch.setattr(cli, "_runtime", lambda *args, **kwargs: runtime)

    result = CliRunner().invoke(cli.app, ["ask", "How", "many", "orders?"])

    assert result.exit_code == 1
    assert "provider detail" not in result.output
    assert "Traceback" not in result.output
    assert runtime.calls == 1


def test_chat_continues_after_model_failure(test_config, tmp_path, monkeypatch):
    runtime = _failing_runtime(test_config, tmp_path)
    inputs = iter(["How many orders?", "exit"])
    monkeypatch.setattr(cli, "_runtime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(cli.console, "input", lambda prompt: next(inputs))

    cli.chat()

    assert runtime.calls == 1


def test_quality_replay_cli_passes():
    result = CliRunner().invoke(
        cli.app,
        ["eval", "--suite", "quality", "--mode", "replay"],
    )

    assert result.exit_code == 0, result.output


def test_eval_rejects_unknown_suite():
    result = CliRunner().invoke(cli.app, ["eval", "--suite", "unknown"])

    assert result.exit_code != 0
    assert "guardrails" in result.output


def test_eval_rejects_automated_only_for_guardrails():
    result = CliRunner().invoke(cli.app, ["eval", "--automated-only"])

    assert result.exit_code != 0
    assert "applies only to the quality suite" in result.output


def test_quality_automated_only_accepts_pending_human_review(
    test_config, monkeypatch
):
    from pathlib import Path

    from retail_agent.quality_evals import (
        evaluate_quality_case,
        load_quality_cases,
        summarize_quality_results,
    )

    case = load_quality_cases(Path("data/quality_eval_cases.jsonl"))[0]
    replay = case.replay.model_copy(update={"usefulness_score": None})
    pending = summarize_quality_results(
        "replay", [evaluate_quality_case(test_config, case, replay)]
    )
    monkeypatch.setattr(cli, "load_settings", lambda path: test_config)
    monkeypatch.setattr(cli, "run_quality_replay_evals", lambda config, path: pending)

    automated = CliRunner().invoke(
        cli.app,
        ["eval", "--suite", "quality", "--mode", "replay", "--automated-only"],
    )
    release = CliRunner().invoke(
        cli.app,
        ["eval", "--suite", "quality", "--mode", "replay"],
    )

    assert automated.exit_code == 0
    assert "AUTO PASS" in automated.output
    assert release.exit_code == 1


def test_bq_smoke_renders_success(test_config, tmp_path, monkeypatch):
    runtime = SimpleNamespace(
        bigquery_smoke=lambda sql: SimpleNamespace(
            query=QueryResult(
                sql=sql,
                rows=[{"order_item_rows": 10}],
                total_rows=1,
                dry_run_bytes=100,
                job_id="job-1",
            ),
            trace_id="trace",
            project=None,
            dataset=test_config.bigquery.dataset,
        )
    )
    monkeypatch.setattr(cli, "_runtime", lambda *args, **kwargs: runtime)

    cli.bq_smoke()


def test_bq_smoke_converts_query_failure_to_clean_exit(test_config, tmp_path, monkeypatch):
    def fail(sql):
        raise RuntimeOperationError("warehouse unavailable")

    monkeypatch.setattr(
        cli,
        "_runtime",
        lambda *args, **kwargs: SimpleNamespace(bigquery_smoke=fail),
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli.bq_smoke()

    assert exc_info.value.exit_code == 1


def test_index_golden_uses_runtime_store(monkeypatch):
    class Runtime:
        def index_golden(self, *, recreate=False):
            assert recreate is True
            return 1

    runtime = Runtime()
    monkeypatch.setattr(cli, "_runtime", lambda *args, **kwargs: runtime)

    cli.index_golden(recreate=True)


def test_default_guardrail_eval_cli_passes():
    result = CliRunner().invoke(cli.app, ["eval"])

    assert result.exit_code == 0, result.output
