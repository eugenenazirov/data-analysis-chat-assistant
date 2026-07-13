from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from retail_agent.application.dto import AgentFailure, AnalysisResponse


def render_report(console: Console, report: AnalysisResponse) -> None:
    if isinstance(report, AgentFailure):
        console.print(f"[bold red]Analysis unavailable:[/bold red] {report.message}")
        if report.retryable:
            console.print("[yellow]This failure may be temporary; retry is safe.[/yellow]")
        console.print(f"[dim]trace_id={report.trace_id}[/dim]")
        return

    if report.refused:
        console.print(f"[bold red]Refused:[/bold red] {report.answer}")
        console.print(f"[dim]trace_id={report.trace_id}[/dim]")
        return

    title = "## Degraded result" if report.degraded else "## Answer"
    console.print(Markdown(f"{title}\n{report.answer}"))
    _render_list_section(console, "Highlights", report.highlights)
    if report.table:
        _render_table(console, report.table)
    _render_list_section(console, "Assumptions", report.assumptions)
    _render_list_section(console, "Caveats", report.caveats)
    _render_list_section(console, "Follow-ups", report.followups)
    if report.sql:
        console.print(Markdown(f"## SQL\n```sql\n{report.sql}\n```"))
    if report.chart_artifact is not None:
        console.print(
            f"[bold]Chart:[/bold] {report.chart_artifact.path} "
            f"[dim]({report.chart_artifact.output_format}, "
            f"{report.chart_artifact.size_bytes} bytes)[/dim]"
        )
    console.print(f"[dim]trace_id={report.trace_id}[/dim]")


def _render_table(console: Console, rows: list[dict[str, object]]) -> None:
    columns = list(rows[0].keys())
    table = Table(show_lines=False)
    for column in columns:
        table.add_column(str(column))
    for row in rows:
        table.add_row(*(str(row.get(column, "")) for column in columns))
    console.print(table)


def _render_list_section(console: Console, title: str, items: list[str]) -> None:
    if items:
        console.print(Markdown(f"## {title}\n" + "\n".join(f"- {item}" for item in items)))
