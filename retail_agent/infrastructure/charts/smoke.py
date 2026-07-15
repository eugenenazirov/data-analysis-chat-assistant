from __future__ import annotations

from dataclasses import dataclass

from retail_agent.domain.models import ChartArtifact, ChartRequest
from retail_agent.infrastructure.charts.local_python_executor import (
    LocalPythonChartExecutor,
)
from retail_agent.infrastructure.charts.templates import TESTED_CHART_TEMPLATES
from retail_agent.infrastructure.settings import ChartExecutionSettings


@dataclass(frozen=True)
class ChartSmokeArtifact:
    case: str
    artifact: ChartArtifact


async def run_chart_smoke(
    settings: ChartExecutionSettings,
) -> tuple[ChartSmokeArtifact, ...]:
    """Exercise every documented chart dependency through the production executor."""

    executor = LocalPythonChartExecutor(settings)
    artifacts: list[ChartSmokeArtifact] = []
    for name, request in _smoke_requests():
        artifact = await executor.execute(request)
        artifacts.append(ChartSmokeArtifact(case=name, artifact=artifact))
    return tuple(artifacts)


def _smoke_requests() -> tuple[tuple[str, ChartRequest], ...]:
    monthly_rows = [
        {"month": f"2026-{month:02d}-01", "revenue": month * 1_000}
        for month in range(1, 7)
    ]
    heatmap_rows = [
        {
            "month": f"2026-{month:02d}-01",
            "category": f"Category {category:02d}",
            "revenue": month * category * 100,
        }
        for category in range(1, 27)
        for month in range(1, 7)
    ]
    data_by_case = {
        "matplotlib-png": [{"category": "Outerwear", "revenue": 42_000}],
        "matplotlib-svg": monthly_rows,
        "pandas-line": monthly_rows,
        "seaborn-grouped-bar": heatmap_rows,
        "six-month-category-heatmap": heatmap_rows,
    }
    return tuple(
        (
            template.name,
            ChartRequest(
                code=template.code,
                data=data_by_case[template.name],
                output_format=template.output_format,
            ),
        )
        for template in TESTED_CHART_TEMPLATES
    )
