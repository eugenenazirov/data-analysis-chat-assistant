from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from retail_agent.bootstrap import Runtime, RuntimeOperationError
from retail_agent.presentation.cli.renderer import render_report

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


def _runtime(config_path: str, *, require_retrieval: bool = False) -> Runtime:
    return Runtime.from_config_path(
        config_path,
        require_retrieval=require_retrieval,
    )


@app.command()
def index_golden(
    recreate: Annotated[bool, typer.Option(help="Recreate the Qdrant collection.")] = False,
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
) -> None:
    """Index seed Golden Knowledge trios into Qdrant."""

    count = _runtime(config_path, require_retrieval=True).index_golden(
        recreate=recreate
    )
    console.print(f"Indexed [bold]{count}[/bold] Golden Knowledge trios.")


@app.command()
def ask(
    question: Annotated[list[str], typer.Argument(help="Question to ask.")],
    user: Annotated[str, typer.Option("--user")] = "manager_a",
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
) -> None:
    """Ask one analytics question and print a report."""

    result = asyncio.run(
        _runtime(config_path).analyze(" ".join(question), user_id=user)
    )
    render_report(console, result.response)
    if result.failed:
        raise typer.Exit(code=1)


@app.command("bq-smoke")
def bq_smoke(
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
) -> None:
    """Run a tiny live BigQuery query without Gemini or Golden Knowledge retrieval."""

    try:
        smoke = _runtime(config_path).bigquery_smoke(BIGQUERY_SMOKE_SQL)
    except RuntimeOperationError as exc:
        console.print(f"[bold red]BigQuery smoke test failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    result = smoke.query
    table = Table(title="BigQuery live smoke test")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Project", smoke.project or "(ADC default)")
    table.add_row("Dataset", smoke.dataset)
    table.add_row("Dry-run bytes", str(result.dry_run_bytes or 0))
    table.add_row("Rows returned", str(result.total_rows))
    table.add_row("Job ID", result.job_id or "(not available)")
    for key, value in (result.rows[0] if result.rows else {}).items():
        table.add_row(str(key), str(value))
    console.print(table)
    console.print(Markdown(f"```sql\n{result.sql}\n```"))
    console.print(f"[dim]trace_id={smoke.trace_id}[/dim]")


@app.command()
def chat(
    user: Annotated[str, typer.Option("--user")] = "manager_a",
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
) -> None:
    """Start an interactive CLI chat session."""

    runtime = _runtime(config_path)
    conversation_id = asyncio.run(runtime.start_conversation.execute())
    console.print("[bold]Retail analysis assistant[/bold]. Type 'exit' to stop.")
    while True:
        question = console.input("[bold cyan]you>[/bold cyan] ").strip()
        if question.lower() in {"exit", "quit", ":q"}:
            break
        if not question:
            continue
        result = asyncio.run(
            runtime.analyze(
                question,
                user_id=user,
                conversation_id=conversation_id,
            )
        )
        conversation_id = result.conversation_id
        render_report(console, result.response)
