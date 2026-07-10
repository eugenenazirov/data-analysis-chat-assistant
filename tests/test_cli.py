from __future__ import annotations

from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from retail_agent import cli
from retail_agent.bigquery import QueryExecutionError
from retail_agent.cli import BIGQUERY_SMOKE_SQL
from retail_agent.models import QueryResult
from retail_agent.observability import EventLogger
from retail_agent.sql_guard import validate_and_prepare_sql


class EmptyGoldenStore:
    def search(self, question: str, trace_id: str, limit: int = 3):
        return []


class FakeBigQuery:
    def describe_allowed_tables(self) -> str:
        return "- orders"


class FailingAgent:
    def __init__(self):
        self.calls = 0

    async def run(self, prompt: str, **kwargs):
        self.calls += 1
        raise ConnectionError("provider detail must stay out of the UI")


def _failing_runtime(test_config, tmp_path):
    return SimpleNamespace(
        config=test_config,
        bigquery=FakeBigQuery(),
        golden_store=EmptyGoldenStore(),
        logger=EventLogger(tmp_path / "runs.jsonl"),
        analysis_agent=FailingAgent(),
    )


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
    assert runtime.analysis_agent.calls == 1


def test_chat_continues_after_model_failure(test_config, tmp_path, monkeypatch):
    runtime = _failing_runtime(test_config, tmp_path)
    inputs = iter(["How many orders?", "exit"])
    monkeypatch.setattr(cli, "_runtime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(cli.console, "input", lambda prompt: next(inputs))

    cli.chat()

    assert runtime.analysis_agent.calls == 1


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
    monkeypatch.setattr(cli, "load_config", lambda path: test_config)
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
    test_config.observability.log_path = tmp_path / "smoke.jsonl"

    class SuccessfulRunner:
        def __init__(self, config, logger):
            pass

        def execute(self, sql, trace_id):
            return QueryResult(
                sql=sql,
                rows=[{"order_item_rows": 10}],
                total_rows=1,
                dry_run_bytes=100,
                job_id="job-1",
            )

    monkeypatch.setattr(cli, "load_config", lambda path: test_config)
    monkeypatch.setattr(cli, "BigQueryRunner", SuccessfulRunner)

    cli.bq_smoke()

    assert test_config.observability.log_path.exists()


def test_bq_smoke_converts_query_failure_to_clean_exit(test_config, tmp_path, monkeypatch):
    test_config.observability.log_path = tmp_path / "smoke.jsonl"

    class FailingRunner:
        def __init__(self, config, logger):
            pass

        def execute(self, sql, trace_id):
            raise QueryExecutionError("warehouse unavailable")

    monkeypatch.setattr(cli, "load_config", lambda path: test_config)
    monkeypatch.setattr(cli, "BigQueryRunner", FailingRunner)

    with pytest.raises(typer.Exit) as exc_info:
        cli.bq_smoke()

    assert exc_info.value.exit_code == 1


def test_index_golden_uses_runtime_store(monkeypatch):
    class Store:
        def load_seed_trios(self, path):
            return ["trio"]

        def index(self, trios, recreate=False):
            assert trios == ["trio"]
            assert recreate is True
            return 1

    runtime = SimpleNamespace(
        config=SimpleNamespace(golden_trios_path="trios.jsonl"),
        golden_store=Store(),
    )
    monkeypatch.setattr(cli, "_runtime", lambda *args, **kwargs: runtime)

    cli.index_golden(recreate=True)


def test_default_guardrail_eval_cli_passes():
    result = CliRunner().invoke(cli.app, ["eval"])

    assert result.exit_code == 0, result.output
