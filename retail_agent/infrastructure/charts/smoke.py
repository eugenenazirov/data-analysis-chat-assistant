from __future__ import annotations

from dataclasses import dataclass

from retail_agent.domain.models import ChartArtifact, ChartRequest
from retail_agent.infrastructure.charts.local_python_executor import (
    LocalPythonChartExecutor,
)
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
    return (
        (
            "matplotlib-png",
            ChartRequest(
                code="""
import json
from pathlib import Path
import matplotlib.pyplot as plt

rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
fig, ax = plt.subplots(figsize=(6, 4))
ax.bar([row["category"] for row in rows], [row["revenue"] for row in rows])
fig.tight_layout()
fig.savefig("chart.png", dpi=160, bbox_inches="tight")
""",
                data=[{"category": "Outerwear", "revenue": 42_000}],
            ),
        ),
        (
            "matplotlib-svg",
            ChartRequest(
                code="""
import json
from pathlib import Path
import matplotlib.pyplot as plt

rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
fig, ax = plt.subplots(figsize=(6, 4))
ax.plot([row["month"] for row in rows], [row["revenue"] for row in rows])
fig.tight_layout()
fig.savefig("chart.svg", bbox_inches="tight")
""",
                data=monthly_rows,
                output_format="svg",
            ),
        ),
        (
            "pandas-line",
            ChartRequest(
                code="""
import json
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd

rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
frame = pd.DataFrame(rows)
fig, ax = plt.subplots(figsize=(8, 4))
ax.plot(frame["month"], frame["revenue"], marker="o")
fig.tight_layout()
fig.savefig("chart.png", dpi=160, bbox_inches="tight")
""",
                data=monthly_rows,
            ),
        ),
        (
            "seaborn-bar",
            ChartRequest(
                code="""
import json
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
frame = pd.DataFrame(rows)
fig, ax = plt.subplots(figsize=(8, 4))
sns.barplot(data=frame, x="month", y="revenue", ax=ax)
fig.tight_layout()
fig.savefig("chart.png", dpi=160, bbox_inches="tight")
""",
                data=monthly_rows,
            ),
        ),
        (
            "six-month-category-heatmap",
            ChartRequest(
                code="""
import json
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
frame = pd.DataFrame(rows)
pivot = frame.pivot(index="category", columns="month", values="revenue")
fig, ax = plt.subplots(figsize=(12, 10))
sns.heatmap(pivot, cmap="Blues", ax=ax)
fig.tight_layout()
fig.savefig("chart.png", dpi=160, bbox_inches="tight")
""",
                data=heatmap_rows,
            ),
        ),
    )
