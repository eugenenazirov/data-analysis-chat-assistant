from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from retail_agent.agent import ConversationState, run_question
from retail_agent.bigquery import BigQueryRunner, QueryExecutionError
from retail_agent.bootstrap import Runtime
from retail_agent.config import load_config
from retail_agent.evals import run_guardrail_evals
from retail_agent.models import AgentFailure
from retail_agent.observability import EventLogger, new_trace_id
from retail_agent.quality_evals import (
    load_human_scores,
    run_quality_live_evals,
    run_quality_replay_evals,
    write_quality_report,
)
from retail_agent.rendering import render_report

app = typer.Typer(help="Retail data analysis chat assistant")
console = Console()


class EvalSuite(StrEnum):
    guardrails = "guardrails"
    quality = "quality"


class QualityEvalMode(StrEnum):
    replay = "replay"
    live = "live"

BIGQUERY_SMOKE_SQL = """
SELECT
  COUNT(1) AS order_item_rows,
  COUNT(DISTINCT order_id) AS distinct_orders,
  ROUND(SUM(sale_price), 2) AS gross_item_sales
FROM `bigquery-public-data.thelook_ecommerce.order_items`
WHERE created_at IS NOT NULL
"""


def _runtime(config_path: str, *, require_qdrant: bool = False) -> Runtime:
    config = load_config(config_path)
    runtime = Runtime(config)
    if require_qdrant:
        runtime.golden_store.wait_until_ready()
    return runtime


@app.command()
def index_golden(
    recreate: Annotated[bool, typer.Option(help="Recreate the Qdrant collection.")] = False,
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
) -> None:
    """Index seed Golden Knowledge trios into Qdrant."""

    runtime = _runtime(config_path, require_qdrant=True)
    trios = runtime.golden_store.load_seed_trios(runtime.config.golden_trios_path)
    count = runtime.golden_store.index(trios, recreate=recreate)
    console.print(f"Indexed [bold]{count}[/bold] Golden Knowledge trios.")


@app.command()
def ask(
    question: Annotated[list[str], typer.Argument(help="Question to ask.")],
    user: Annotated[str, typer.Option("--user")] = "manager_a",
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
) -> None:
    """Ask one analytics question and print a report."""

    runtime = _runtime(config_path)
    turn = asyncio.run(
        run_question(
            " ".join(question),
            config=runtime.config,
            bigquery=runtime.bigquery,
            golden_store=runtime.golden_store,
            logger=runtime.logger,
            user_id=user,
            analysis_agent=runtime.analysis_agent,
        )
    )
    render_report(console, turn.response)
    if isinstance(turn.response, AgentFailure):
        raise typer.Exit(code=1)


@app.command("bq-smoke")
def bq_smoke(
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
) -> None:
    """Run a tiny live BigQuery query without Gemini or Golden Knowledge retrieval."""

    config = load_config(config_path)
    logger = EventLogger(config.observability.log_path)
    runner = BigQueryRunner(config, logger)
    trace_id = new_trace_id()
    logger.event(trace_id, "bigquery_smoke_started")

    try:
        result = runner.execute(BIGQUERY_SMOKE_SQL, trace_id)
    except QueryExecutionError as exc:
        logger.event(trace_id, "bigquery_smoke_failed", error=str(exc))
        console.print(f"[bold red]BigQuery smoke test failed:[/bold red] {exc}")
        console.print(f"[dim]trace_id={trace_id}[/dim]")
        raise typer.Exit(code=1) from exc

    logger.event(trace_id, "bigquery_smoke_completed", rows=result.rows)
    table = Table(title="BigQuery live smoke test")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Project", config.bigquery.project or "(ADC default)")
    table.add_row("Dataset", config.bigquery.dataset)
    table.add_row("Dry-run bytes", str(result.dry_run_bytes or 0))
    table.add_row("Rows returned", str(result.total_rows))
    table.add_row("Job ID", result.job_id or "(not available)")
    for key, value in (result.rows[0] if result.rows else {}).items():
        table.add_row(str(key), str(value))
    console.print(table)
    console.print(Markdown(f"```sql\n{result.sql}\n```"))
    console.print(f"[dim]trace_id={trace_id}[/dim]")


@app.command()
def chat(
    user: Annotated[str, typer.Option("--user")] = "manager_a",
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
) -> None:
    """Start an interactive CLI chat session."""

    runtime = _runtime(config_path)
    conversation = ConversationState()
    console.print("[bold]Retail analysis assistant[/bold]. Type 'exit' to stop.")
    while True:
        question = console.input("[bold cyan]you>[/bold cyan] ").strip()
        if question.lower() in {"exit", "quit", ":q"}:
            break
        if not question:
            continue
        turn = asyncio.run(
            run_question(
                question,
                config=runtime.config,
                bigquery=runtime.bigquery,
                golden_store=runtime.golden_store,
                logger=runtime.logger,
                user_id=user,
                conversation=conversation,
                analysis_agent=runtime.analysis_agent,
            )
        )
        conversation = turn.conversation
        render_report(console, turn.response)


@app.command()
def eval(
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
    suite: Annotated[EvalSuite, typer.Option()] = EvalSuite.guardrails,
    mode: Annotated[
        QualityEvalMode, typer.Option(help="Quality suite execution mode.")
    ] = QualityEvalMode.replay,
    cases_path: Annotated[
        Path, typer.Option("--cases", help="Quality evaluation JSONL dataset.")
    ] = Path("data/quality_eval_cases.jsonl"),
    output: Annotated[
        Path | None, typer.Option(help="Optional machine-readable quality report.")
    ] = None,
    human_scores: Annotated[
        Path | None,
        typer.Option(help="JSON mapping case IDs to analyst usefulness scores (0-5)."),
    ] = None,
    automated_only: Annotated[
        bool,
        typer.Option(
            help="Gate automated quality metrics without requiring analyst scores."
        ),
    ] = False,
) -> None:
    """Run deterministic guardrails or replay/live answer-quality evaluations."""

    config = load_config(config_path)
    if suite is EvalSuite.quality:
        if mode is QualityEvalMode.replay:
            result = run_quality_replay_evals(config, cases_path)
        else:
            runtime = _runtime(config_path)
            result = asyncio.run(
                run_quality_live_evals(
                    config,
                    cases_path,
                    bigquery=runtime.bigquery,
                    golden_store=runtime.golden_store,
                    logger=runtime.logger,
                    analysis_agent=runtime.analysis_agent,
                    human_scores=load_human_scores(human_scores),
                )
            )
            output = output or Path("artifacts/quality-eval-live.json")
        table = Table(title=f"Answer-quality evals ({result.mode})")
        table.add_column("Eval")
        table.add_column("Status")
        table.add_column("Scores")
        for case_result in result.results:
            table.add_row(
                case_result.name,
                (
                    "PASS"
                    if case_result.passed
                    else "AUTO PASS"
                    if case_result.automated_passed
                    else "FAIL"
                ),
                case_result.detail,
            )
        console.print(table)
        if result.needs_human_review:
            console.print(
                "[yellow]Human usefulness scores are required before release.[/yellow]"
            )
        if output is not None:
            write_quality_report(result, output)
            console.print(f"[dim]quality_report={output}[/dim]")
        gate_passed = result.automated_passed if automated_only else result.passed
        if not gate_passed:
            raise typer.Exit(code=1)
        return

    if automated_only:
        raise typer.BadParameter("--automated-only applies only to the quality suite.")

    results = run_guardrail_evals(config)
    table = Table(title="Guardrail evals")
    table.add_column("Eval")
    table.add_column("Status")
    table.add_column("Detail")
    for result in results:
        table.add_row(result.name, "PASS" if result.passed else "FAIL", result.detail)
    console.print(table)
    if not all(result.passed for result in results):
        raise typer.Exit(code=1)
