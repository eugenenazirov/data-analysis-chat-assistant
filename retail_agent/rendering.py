from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from retail_agent.models import AnalysisReport


def render_report(console: Console, report: AnalysisReport) -> None:
    if report.refused:
        console.print(f"[bold red]Refused:[/bold red] {report.answer}")
        console.print(f"[dim]trace_id={report.trace_id}[/dim]")
        return

    console.print(Markdown(f"## Answer\n{report.answer}"))
    if report.highlights:
        console.print(Markdown("## Highlights\n" + "\n".join(f"- {item}" for item in report.highlights)))
    if report.table:
        _render_table(console, report.table)
    if report.assumptions:
        console.print(Markdown("## Assumptions\n" + "\n".join(f"- {item}" for item in report.assumptions)))
    if report.caveats:
        console.print(Markdown("## Caveats\n" + "\n".join(f"- {item}" for item in report.caveats)))
    if report.followups:
        console.print(Markdown("## Follow-ups\n" + "\n".join(f"- {item}" for item in report.followups)))
    if report.sql:
        console.print(Markdown(f"## SQL\n```sql\n{report.sql}\n```"))
    console.print(f"[dim]trace_id={report.trace_id}[/dim]")


def _render_table(console: Console, rows: list[dict[str, object]]) -> None:
    columns = list(rows[0].keys())
    table = Table(show_lines=False)
    for column in columns:
        table.add_column(str(column))
    for row in rows:
        table.add_row(*(str(row.get(column, "")) for column in columns))
    console.print(table)
