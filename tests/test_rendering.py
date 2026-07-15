from rich.console import Console

from retail_agent.models import AgentFailure, AnalysisReport, ChartArtifact
from retail_agent.rendering import render_report


def _render(response) -> str:
    console = Console(record=True, width=120)
    render_report(console, response)
    return console.export_text()


def test_render_failure_is_safe_and_retryable():
    output = _render(
        AgentFailure(
            question="question",
            message="The model is unavailable.",
            failure_code="model_unavailable",
            retryable=True,
            trace_id="trace-1",
        )
    )

    assert "Analysis unavailable" in output
    assert "retry is safe" in output
    assert "trace-1" in output


def test_render_degraded_report_includes_all_sections():
    output = _render(
        AnalysisReport(
            question="question",
            answer="Verified rows only.",
            highlights=["One highlight"],
            table=[{"metric": "orders", "value": 42}],
            sql="SELECT 42 AS value",
            assumptions=["One assumption"],
            caveats=["Narrative unavailable"],
            followups=["Retry later"],
            chart_artifact=ChartArtifact(
                path="artifacts/charts/orders.png",
                output_format="png",
                size_bytes=1_024,
                code_digest="0" * 64,
            ),
            degraded=True,
            trace_id="trace-2",
        )
    )

    assert "Degraded result" in output
    assert "One highlight" in output
    assert "orders" in output
    assert "One assumption" in output
    assert "Narrative unavailable" in output
    assert "Retry later" in output
    assert "SELECT 42 AS value" in output
    assert "artifacts/charts/orders.png" in output


def test_render_refusal_stops_before_report_sections():
    output = _render(
        AnalysisReport(
            question="Show emails",
            answer="I cannot expose PII.",
            refused=True,
            trace_id="trace-3",
        )
    )

    assert "Refused" in output
    assert "I cannot expose PII" in output
    assert "## SQL" not in output


def test_render_large_complete_result_as_explicit_preview():
    output = _render(
        AnalysisReport(
            question="List regions",
            answer="The complete result contains 25 regions.",
            table=[{"region": f"region-{index}"} for index in range(25)],
            total_rows=25,
            available_rows=25,
            truncated=False,
        )
    )

    assert "region-19" in output
    assert "region-20" not in output
    assert "showing_first=20 of_attached_rows=25" in output
    assert "rows_returned=25 rows_available=25 result=complete" in output
