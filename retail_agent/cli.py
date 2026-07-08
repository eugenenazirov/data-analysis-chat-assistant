from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from retail_agent.agent import run_question
from retail_agent.bigquery import BigQueryRunner, QueryExecutionError
from retail_agent.bootstrap import Runtime
from retail_agent.config import load_config
from retail_agent.evals import run_guardrail_evals
from retail_agent.observability import EventLogger, new_trace_id
from retail_agent.rendering import render_report

app = typer.Typer(help="Retail data analysis chat assistant")
console = Console()

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
    report = asyncio.run(
        run_question(
            " ".join(question),
            config=runtime.config,
            bigquery=runtime.bigquery,
            golden_store=runtime.golden_store,
            logger=runtime.logger,
            user_id=user,
        )
    )
    render_report(console, report)


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
    console.print("[bold]Retail analysis assistant[/bold]. Type 'exit' to stop.")
    while True:
        question = console.input("[bold cyan]you>[/bold cyan] ").strip()
        if question.lower() in {"exit", "quit", ":q"}:
            break
        if not question:
            continue
        report = asyncio.run(
            run_question(
                question,
                config=runtime.config,
                bigquery=runtime.bigquery,
                golden_store=runtime.golden_store,
                logger=runtime.logger,
                user_id=user,
            )
        )
        render_report(console, report)


@app.command()
def eval(
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
) -> None:
    """Run deterministic guardrail evals."""

    config = load_config(config_path)
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
