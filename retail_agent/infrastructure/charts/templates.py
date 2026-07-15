from __future__ import annotations

from dataclasses import dataclass

from retail_agent.domain.models import ChartFormat


@dataclass(frozen=True)
class ChartCodeTemplate:
    name: str
    description: str
    code: str
    output_format: ChartFormat


TESTED_CHART_TEMPLATES = (
    ChartCodeTemplate(
        name="matplotlib-png",
        description="PNG bar chart for one category dimension and one numeric measure",
        output_format="png",
        code='''import json
from pathlib import Path
import matplotlib.pyplot as plt

rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
fig, ax = plt.subplots(figsize=(6, 4))
ax.bar([row["category"] for row in rows], [row["revenue"] for row in rows])
fig.tight_layout()
fig.savefig("chart.png", dpi=160, bbox_inches="tight")
''',
    ),
    ChartCodeTemplate(
        name="matplotlib-svg",
        description="explicitly requested SVG line chart",
        output_format="svg",
        code='''import json
from pathlib import Path
import matplotlib.pyplot as plt

rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
fig, ax = plt.subplots(figsize=(6, 4))
ax.plot([row["month"] for row in rows], [row["revenue"] for row in rows])
fig.tight_layout()
fig.savefig("chart.svg", format="svg", bbox_inches="tight")
''',
    ),
    ChartCodeTemplate(
        name="pandas-line",
        description="PNG chronological line chart using pandas",
        output_format="png",
        code='''import json
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd

rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
frame = pd.DataFrame(rows)
fig, ax = plt.subplots(figsize=(8, 4))
ax.plot(frame["month"], frame["revenue"], marker="o")
fig.tight_layout()
fig.savefig("chart.png", dpi=160, bbox_inches="tight")
''',
    ),
    ChartCodeTemplate(
        name="seaborn-grouped-bar",
        description="PNG grouped bar chart for month, category, and revenue",
        output_format="png",
        code='''import json
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
frame = pd.DataFrame(rows)
fig, ax = plt.subplots(figsize=(10, 6))
sns.barplot(data=frame, x="month", y="revenue", hue="category", ax=ax)
fig.tight_layout()
fig.savefig("chart.png", dpi=160, bbox_inches="tight")
''',
    ),
    ChartCodeTemplate(
        name="six-month-category-heatmap",
        description="PNG heatmap retaining all 156 cells in the month/category matrix",
        output_format="png",
        code='''import json
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
''',
    ),
)


def render_tested_chart_templates() -> str:
    return "\n\n".join(
        f"{template.description}:\n\n```python\n{template.code.rstrip()}\n```"
        for template in TESTED_CHART_TEMPLATES
    )
