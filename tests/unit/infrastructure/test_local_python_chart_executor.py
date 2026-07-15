from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from retail_agent.domain.errors import ChartExecutionError
from retail_agent.domain.models import ChartRequest
from retail_agent.infrastructure.charts import LocalPythonChartExecutor
from retail_agent.infrastructure.charts.smoke import run_chart_smoke
from retail_agent.infrastructure.settings import ChartExecutionSettings


def _settings(tmp_path: Path, **updates) -> ChartExecutionSettings:
    values = {
        "artifact_directory": tmp_path / "artifacts",
        "max_captured_output_bytes": 1_024,
        "max_output_bytes": 10_000,
        "max_source_bytes": 5_000,
        "timeout_seconds": 1.0,
    }
    values.update(updates)
    return ChartExecutionSettings(**values)


def _executor(tmp_path: Path, **updates) -> tuple[LocalPythonChartExecutor, Path]:
    temporary_root = tmp_path / "temporary"
    temporary_root.mkdir()
    return (
        LocalPythonChartExecutor(
            _settings(tmp_path, **updates),
            temporary_root=temporary_root,
        ),
        temporary_root,
    )


def _svg_code(extra: str = "") -> str:
    return f"""
import json
from pathlib import Path

rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
value = rows[0]["order_count"]
Path("chart.svg").write_text(
    f'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="40"><text>{{value}}</text></svg>',
    encoding="utf-8",
)
{extra}
"""


def test_executor_publishes_valid_svg_without_inheriting_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("RETAIL_CHART_TEST_SECRET", "must-not-leak")
    executor, temporary_root = _executor(tmp_path)

    artifact = asyncio.run(
        executor.execute(
            ChartRequest(
                code=_svg_code(),
                data=[{"order_count": 42}],
                output_format="svg",
            )
        )
    )

    published = Path(artifact.path)
    assert published.is_file()
    assert "42" in published.read_text(encoding="utf-8")
    assert artifact.output_format == "svg"
    assert artifact.size_bytes == published.stat().st_size
    assert len(artifact.code_digest) == 64
    assert list(temporary_root.iterdir()) == []


def test_executor_generates_png_with_runtime_chart_library(tmp_path):
    executor, temporary_root = _executor(tmp_path, timeout_seconds=5)
    code = """
import json
from pathlib import Path
import matplotlib.pyplot as plt

rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
plt.bar(["orders"], [rows[0]["order_count"]])
plt.savefig("chart.png")
"""

    artifact = asyncio.run(
        executor.execute(
            ChartRequest(
                code=code,
                data=[{"order_count": 42}],
                output_format="png",
            )
        )
    )

    published = Path(artifact.path)
    assert published.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert artifact.output_format == "png"
    assert list(temporary_root.iterdir()) == []


def test_chart_smoke_exercises_all_supported_libraries_and_heatmap(tmp_path):
    settings = _settings(
        tmp_path,
        timeout_seconds=15,
        max_output_bytes=5_000_000,
    )

    artifacts = asyncio.run(run_chart_smoke(settings))

    assert [item.case for item in artifacts] == [
        "matplotlib-png",
        "matplotlib-svg",
        "pandas-line",
        "seaborn-bar",
        "six-month-category-heatmap",
    ]
    assert all(Path(item.artifact.path).is_file() for item in artifacts)
    assert {item.artifact.output_format for item in artifacts} == {"png", "svg"}


def test_executor_runs_grouped_bar_template_with_pandas(tmp_path):
    executor, temporary_root = _executor(
        tmp_path,
        timeout_seconds=10,
        max_output_bytes=5_000_000,
    )
    code = """
import json
from pathlib import Path
import matplotlib.pyplot as plt
import pandas as pd

rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
frame = pd.DataFrame(rows)
pivot = frame.pivot(index="month", columns="category", values="revenue")
fig, ax = plt.subplots(figsize=(8, 4))
pivot.plot.bar(ax=ax)
fig.tight_layout()
fig.savefig("chart.png", dpi=160, bbox_inches="tight")
"""

    artifact = asyncio.run(
        executor.execute(
            ChartRequest(
                code=code,
                data=[
                    {"month": "2026-01", "category": "A", "revenue": 10},
                    {"month": "2026-01", "category": "B", "revenue": 20},
                    {"month": "2026-02", "category": "A", "revenue": 15},
                    {"month": "2026-02", "category": "B", "revenue": 25},
                ],
            )
        )
    )

    assert Path(artifact.path).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert list(temporary_root.iterdir()) == []


def test_executor_times_out_and_cleans_temporary_directory(tmp_path):
    executor, temporary_root = _executor(tmp_path, timeout_seconds=0.05)

    with pytest.raises(ChartExecutionError) as exc_info:
        asyncio.run(
            executor.execute(
                ChartRequest(
                    code="import time\ntime.sleep(2)",
                    data=[{"order_count": 42}],
                    output_format="svg",
                )
            )
        )

    assert exc_info.value.code == "timeout"
    assert list(temporary_root.iterdir()) == []


def test_executor_cancellation_reaps_process_and_reader_tasks(tmp_path):
    executor, temporary_root = _executor(tmp_path, timeout_seconds=5)

    async def cancel_execution():
        task = asyncio.create_task(
            executor.execute(
                ChartRequest(
                    code="import time\ntime.sleep(5)",
                    data=[{"order_count": 42}],
                    output_format="svg",
                )
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_execution())

    assert list(temporary_root.iterdir()) == []


@pytest.mark.parametrize(
    ("code", "expected_code"),
    [
        ("print('no artifact')", "output_missing"),
        (
            "from pathlib import Path\nPath('chart.svg').write_text('<svg><script/></svg>')",
            "invalid_output",
        ),
        (
            "from pathlib import Path\n"
            "Path('chart.svg').write_text("
            "'<svg><style>@import url(https://example.com/x.css);</style></svg>')",
            "invalid_output",
        ),
        (
            "from pathlib import Path\n"
            "Path('chart.svg').write_text("
            "'<!DOCTYPE svg SYSTEM \"https://example.com/evil.dtd\"><svg/>')",
            "invalid_output",
        ),
        (
            "print('x' * 2048)\n"
            "from pathlib import Path\n"
            "Path('chart.svg').write_text('<svg xmlns=\"http://www.w3.org/2000/svg\"/>')",
            "captured_output_limit",
        ),
    ],
)
def test_executor_rejects_invalid_process_outputs(tmp_path, code, expected_code):
    executor, temporary_root = _executor(tmp_path)

    with pytest.raises(ChartExecutionError) as exc_info:
        asyncio.run(executor.execute(ChartRequest(code=code, data=[], output_format="svg")))

    assert exc_info.value.code == expected_code
    assert list(temporary_root.iterdir()) == []


def test_executor_rejects_oversized_source_before_starting_process(tmp_path):
    executor, temporary_root = _executor(tmp_path, max_source_bytes=1_000)

    with pytest.raises(ChartExecutionError) as exc_info:
        asyncio.run(executor.execute(ChartRequest(code="#" * 1_001, data=[], output_format="png")))

    assert exc_info.value.code == "source_too_large"
    assert list(temporary_root.iterdir()) == []


def test_executor_classifies_syntax_failure_with_line_hint(tmp_path):
    executor, temporary_root = _executor(tmp_path)

    with pytest.raises(ChartExecutionError) as exc_info:
        asyncio.run(
            executor.execute(
                ChartRequest(code="if True print('broken')", data=[], output_format="png")
            )
        )

    assert exc_info.value.code == "syntax_error"
    assert "line 1" in (exc_info.value.repair_hint or "")
    assert list(temporary_root.iterdir()) == []


def test_executor_classifies_missing_allowed_dependency(tmp_path):
    executor, temporary_root = _executor(tmp_path)

    with pytest.raises(ChartExecutionError) as exc_info:
        asyncio.run(
            executor.execute(
                ChartRequest(
                    code="import pandas.definitely_missing",
                    data=[{"revenue": 42}],
                    output_format="png",
                )
            )
        )

    assert exc_info.value.code == "missing_dependency"
    assert "chart.png" in (exc_info.value.repair_hint or "")
    assert "revenue" in (exc_info.value.repair_hint or "")
    assert list(temporary_root.iterdir()) == []


@pytest.mark.parametrize(
    ("statement", "expected_code"),
    [
        ('raise KeyError("missing")', "data_shape_error"),
        ('raise TypeError("wrong shape")', "data_shape_error"),
        ('raise RuntimeError("plot failed")', "runtime_error"),
    ],
)
def test_executor_returns_bounded_actionable_runtime_diagnostics(
    tmp_path, statement, expected_code
):
    executor, temporary_root = _executor(tmp_path)
    code = f"""
import json
from pathlib import Path
rows = json.loads(Path("input.json").read_text(encoding="utf-8"))
{statement}
"""

    with pytest.raises(ChartExecutionError) as exc_info:
        asyncio.run(
            executor.execute(
                ChartRequest(
                    code=code,
                    data=[{"month": "2026-01", "revenue": 42}],
                    output_format="svg",
                )
            )
        )

    hint = exc_info.value.repair_hint or ""
    assert exc_info.value.code == expected_code
    assert "line" in hint
    assert "Available columns: month, revenue" in hint
    assert "chart.svg" in hint
    assert len(hint) < 1_000
    assert list(temporary_root.iterdir()) == []


@pytest.mark.parametrize(
    "code",
    [
        "import os\nos.getenv('HOME')",
        "import socket\nsocket.create_connection(('example.com', 443))",
        "import subprocess\nsubprocess.run(['id'])",
        "from pathlib import Path\nPath('/etc/passwd').read_text()",
        "open('/tmp/leak', 'w').write('x')",
        "__import__('os').environ",
    ],
)
def test_executor_rejects_unsafe_chart_source_before_starting_process(tmp_path, code):
    executor, temporary_root = _executor(tmp_path)

    with pytest.raises(ChartExecutionError) as exc_info:
        asyncio.run(executor.execute(ChartRequest(code=code, data=[], output_format="svg")))

    assert exc_info.value.code == "unsafe_source"
    assert list(temporary_root.iterdir()) == []
