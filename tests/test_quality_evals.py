import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from evals.quality import (
    QualityExpectations,
    ResultContract,
    RetrievalContract,
    _intent_assessment,
    _intent_score,
    _intent_signature,
    _retrieval_assessment,
    _retrieval_scores,
    _row_score,
    evaluate_quality_case,
    load_quality_cases,
    run_quality_live_evals,
    run_quality_replay_evals,
    summarize_quality_results,
)
from retail_agent.agent import TurnResult
from retail_agent.domain.policies.report_evidence import assess_report_evidence
from retail_agent.models import AgentFailure, AnalysisReport, OperationalMetrics
from retail_agent.observability import EventLogger

CASES_PATH = Path("evals/datasets/smoke.jsonl")


def _faithfulness_score(report, rows, sql, tolerance):
    return assess_report_evidence(report, rows, sql, tolerance).score


def test_smoke_replay_suite_remains_comparable(test_config):
    result = run_quality_replay_evals(test_config, CASES_PATH)

    assert result.passed is True
    assert result.aggregate.intent == 1
    assert result.aggregate.calculation == 1
    assert result.aggregate.retrieval == 1
    assert result.aggregate.retrieval_mrr == 1
    assert result.aggregate.retrieval_ndcg == 1
    assert result.aggregate.retrieval_usefulness == 1
    assert result.aggregate.retrieval_harm == 1
    assert result.aggregate.faithfulness == 1
    assert result.aggregate.multi_turn == 1
    assert result.aggregate.usefulness is not None
    assert result.aggregate.usefulness >= 0.8
    assert result.case_count == 4
    assert result.suite_counts == {"smoke": 4}
    assert result.metrics["multi_turn"].applicable_cases == 1
    assert result.metrics["multi_turn"].passed_cases == 1
    assert result.results[0].scores.multi_turn is None
    assert result.results[-1].scores.multi_turn == 1
    assert result.versions.dataset_sha256 != "unknown"
    assert result.versions.prompt == "analysis-v10"


def test_quality_eval_rejects_unsupported_numeric_claim(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    replay = case.replay.model_copy(
        update={"report": case.replay.report.model_copy(update={"answer": "Revenue was 99999999."})}
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.passed is False
    assert result.scores.faithfulness == 0


def test_quality_eval_rejects_truncated_candidate_as_release_success(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    replay = case.replay.model_copy(
        update={
            "available_rows": len(case.replay.candidate_rows) + 1,
            "truncated": True,
            "row_limit": len(case.replay.candidate_rows),
            "report": case.replay.report.model_copy(update={"truncated": True}),
        }
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.automated_passed is False
    assert "complete=no" in result.detail
    assert result.diagnostics.truncated is True


def test_quality_eval_requires_verified_artifact_for_chart_case(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    assert case.expected_chart_format == "png"
    replay = case.replay.model_copy(
        update={"report": case.replay.report.model_copy(update={"chart_artifact": None})}
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.automated_passed is False
    assert "chart=missing" in result.detail


def test_quality_eval_marks_non_chart_case_as_not_required(test_config):
    case = load_quality_cases(CASES_PATH)[1]
    assert case.expected_chart_format is None

    result = evaluate_quality_case(test_config, case, case.replay)

    assert "chart=not-required" in result.detail


@pytest.mark.parametrize("field", ["assumptions", "caveats", "followups"])
def test_faithfulness_checks_every_rendered_narrative_field(field):
    report = AnalysisReport(
        question="How many orders?",
        answer="There were 42 orders.",
        **{field: ["Revenue was 999."]},
    )

    assessment = assess_report_evidence(
        report,
        [{"orders": 42}],
        "SELECT 42 AS orders",
    )

    assert assessment.is_supported is False
    assert assessment.unsupported_numeric_claims == (999.0,)


def test_faithfulness_parses_scientific_notation_as_one_claim():
    report = AnalysisReport(question="Revenue?", answer="Revenue was $1.2e6.")

    unsupported = assess_report_evidence(
        report,
        [{"revenue": 1.2}],
        "SELECT 1.2 AS revenue",
    )
    supported = assess_report_evidence(
        report,
        [{"revenue": 1_200_000}],
        "SELECT 1200000 AS revenue",
    )

    assert unsupported.is_supported is False
    assert unsupported.unsupported_numeric_claims == (1_200_000.0,)
    assert supported.is_supported is True


def test_faithfulness_accepts_numeric_calendar_dimension_from_returned_row():
    report = AnalysisReport(
        question="Which month?",
        answer="The verified month was 2026-06-01.",
    )

    assessment = assess_report_evidence(
        report,
        [{"month": "2026-06-01"}],
        "SELECT DATE '2026-06-01' AS month",
    )

    assert assessment.is_supported is True
    assert assessment.unsupported_numeric_claims == ()


def test_faithfulness_accepts_numeric_calendar_dimension_from_date_object():
    report = AnalysisReport(
        question="Which month?",
        answer="The verified month was 2026-06-01.",
    )

    assessment = assess_report_evidence(
        report,
        [{"month": date(2026, 6, 1)}],
        "SELECT DATE '2026-06-01' AS month",
    )

    assert assessment.is_supported is True
    assert assessment.unsupported_numeric_claims == ()


@pytest.mark.parametrize(
    "answer",
    [
        f"Revenue was {datetime.now(UTC).year}.",
        f"Revenue was ${datetime.now(UTC).year:,}.",
        f"Revenue was {datetime.now(UTC).year} this year.",
        f"Revenue in calendar year was ${datetime.now(UTC).year:,}.",
        f"Revenue this year was {datetime.now(UTC).year}.",
        f"Revenue this year was {datetime.now(UTC).year} dollars.",
        f"Revenue was in {datetime.now(UTC).year} dollars.",
        "Revenue for the top result was 10.",
        "Revenue for the top result was 10 dollars.",
        "Revenue was top 10 dollars.",
        "The returned products represent the top 10% by revenue.",
        "The returned products represent the top 10 percent by revenue.",
        f"Revenue was in {datetime.now(UTC).year}€.",
        "Revenue was USD10M.",
        "Revenue was USD99999999.",
    ],
)
def test_quality_eval_rejects_sql_context_value_as_revenue(test_config, answer):
    case = load_quality_cases(CASES_PATH)[0]
    replay = case.replay.model_copy(
        update={"report": case.replay.report.model_copy(update={"answer": answer})}
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.automated_passed is False
    assert result.scores.faithfulness == 0


@pytest.mark.parametrize(
    "answer,rows",
    [
        ("The return rate was 0.2%.", [{"return_rate": 0.2}]),
        ("Order volume was 1,234%.", [{"orders": 1_234}]),
        ("Revenue increased 200%.", [{"revenue": 500}, {"revenue": 300}]),
        ("There were $10 orders.", [{"orders": 10}]),
        ("Revenue was $2.", [{"revenue": 500}, {"revenue": 250}]),
    ],
)
def test_quality_eval_rejects_unit_incompatible_numeric_claim(test_config, answer, rows):
    case = load_quality_cases(CASES_PATH)[0]
    replay = case.replay.model_copy(
        update={
            "candidate_rows": rows,
            "canonical_rows": rows,
            "report": case.replay.report.model_copy(update={"answer": answer}),
        }
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.automated_passed is False
    assert result.scores.faithfulness == 0


@pytest.mark.parametrize(
    "answer,rows",
    [
        ("The return rate was 20%.", [{"return_rate": 0.2}]),
        ("Revenue increased 66.7%.", [{"revenue": 500}, {"revenue": 300}]),
        ("Revenue increased by $200.", [{"revenue": 500}, {"revenue": 300}]),
    ],
)
def test_quality_eval_accepts_unit_compatible_numeric_claim(test_config, answer, rows):
    source_case = load_quality_cases(CASES_PATH)[0]
    case = source_case.model_copy(update={"evaluators": source_case.evaluators - {"calculation"}})
    replay = case.replay.model_copy(
        update={
            "candidate_rows": rows,
            "canonical_rows": rows,
            "report": case.replay.report.model_copy(update={"answer": answer}),
        }
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.automated_passed is True
    assert result.scores.faithfulness == 1


def test_report_evidence_accepts_percentage_from_prior_verified_turn():
    report = AnalysisReport(
        question="What percentage did it represent?",
        answer="California represented 2.15% of completed orders (2 of 93).",
        sql="SELECT 93 AS total_completed_orders",
    )

    assessment = assess_report_evidence(
        report,
        [{"total_completed_orders": 93}],
        "SELECT 93 AS total_completed_orders",
        prior_verified_rows=[{"region": "California", "completed_orders": 2}],
    )

    assert assessment.is_supported is True
    assert assessment.unsupported_numeric_claims == ()


def test_report_evidence_accepts_having_threshold_in_matching_context():
    report = AnalysisReport(
        question="Which products have return risk?",
        answer="The result includes products with at least 20 items sold.",
        sql="SELECT items_sold FROM products HAVING items_sold >= 20",
    )

    assessment = assess_report_evidence(
        report,
        [{"items_sold": 21}],
        "SELECT items_sold FROM products HAVING items_sold >= 20",
    )

    assert assessment.is_supported is True


def test_report_evidence_accepts_compact_having_threshold_context():
    report = AnalysisReport(
        question="Which products have return risk?",
        answer="The ranking includes products with 20+ items sold.",
        sql="SELECT items_sold FROM products HAVING items_sold >= 20",
    )

    assessment = assess_report_evidence(
        report,
        [{"items_sold": 21}],
        "SELECT items_sold FROM products HAVING items_sold >= 20",
    )

    assert assessment.is_supported is True


def test_report_evidence_binds_colon_delimited_measure_before_claim():
    report = AnalysisReport(
        question="Which products have high return risk?",
        answer=("Product: Trail Jacket; items sold: 21; gross sales: $839.79; return rate: 28.6%."),
    )
    rows = [
        {
            "product_name": "Trail Jacket",
            "items_sold": 21,
            "gross_sales": 839.79,
            "return_rate": 0.286,
        }
    ]

    assessment = assess_report_evidence(
        report,
        rows,
        "SELECT items_sold, gross_sales, return_rate FROM safe_table",
    )

    assert assessment.unsupported_numeric_claims == ()


@pytest.mark.parametrize("marker", ["~", "≈"])
def test_quality_eval_accepts_symbol_rounded_live_regional_report(test_config, marker):
    case = load_quality_cases(CASES_PATH)[-1]
    rows = [
        {"region": "California", "lost_revenue": 5_225.15},
        {"region": "New York", "lost_revenue": 2_774.17},
    ]
    report = case.replay.report.model_copy(
        update={
            "answer": "California lost $5,225.15 versus New York's $2,774.17.",
            "highlights": [f"California's $5,225.15 loss was {marker}1.9x New York's."],
        }
    )
    replay = case.replay.model_copy(
        update={
            "candidate_rows": rows,
            "canonical_rows": rows,
            "report": report,
        }
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.automated_passed is True
    assert result.scores.faithfulness == 1


@pytest.mark.parametrize(
    "identifier_text",
    [
        "customer (ID 67493)",
        "customer ID: 67493",
        "customer (ID: 67493)",
        "customer #67493",
        "customer ID #67493",
    ],
)
def test_quality_eval_accepts_structurally_bound_live_customer_id(test_config, identifier_text):
    case = load_quality_cases(CASES_PATH)[1]
    rows = [{"customer_id": 67_493, "orders": 2, "total_spend": 1_549.39}]
    report = case.replay.report.model_copy(
        update={
            "answer": "Here is our top spending customer:",
            "highlights": [f"Our top spending {identifier_text} spent $1549.39 across 2 orders."],
        }
    )
    replay = case.replay.model_copy(
        update={
            "candidate_rows": rows,
            "canonical_rows": rows,
            "report": report,
        }
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.automated_passed is True
    assert result.scores.faithfulness == 1


def test_faithfulness_rejects_ambiguous_generic_numeric_id():
    report = AnalysisReport(question="question", answer="ID 67493.")

    score = _faithfulness_score(
        report,
        [{"customer_id": 67_493, "order_id": 123}],
        "SELECT customer_id, order_id FROM table",
        tolerance=0.001,
    )

    assert score == 0


def test_faithfulness_accepts_counts_derived_from_returned_dimensions():
    rows = [{"product_name": f"Jeans product {index}", "category": "Jeans"} for index in range(9)]
    rows.extend(
        {"product_name": f"Socks product {index}", "category": "Socks"} for index in range(6)
    )
    report = AnalysisReport(
        question="question",
        answer="Jeans appears in 9 out of 15 returned products.",
    )

    assert _faithfulness_score(report, rows, "SELECT category FROM table", 0.001) == 1

    unsupported = report.model_copy(
        update={"answer": "Jeans appears in 8 out of 15 returned products."}
    )
    assert _faithfulness_score(unsupported, rows, "SELECT category FROM table", 0.001) < 1


def test_faithfulness_rejects_dimension_count_without_matching_count_structure():
    rows = [
        {"product_name": f"Product {index}", "category": "Jeans", "revenue": 100}
        for index in range(9)
    ]
    rows.extend(
        {"product_name": f"Product {index}", "category": "Socks", "revenue": 100}
        for index in range(9, 15)
    )

    wrong_entity = AnalysisReport(
        question="question",
        answer="Jeans generated 9 customers.",
    )
    wrong_measure = AnalysisReport(
        question="question",
        answer="Revenue was 15 across the returned products.",
    )

    assert _faithfulness_score(wrong_entity, rows, "SELECT * FROM table", 0.001) < 1
    assert _faithfulness_score(wrong_measure, rows, "SELECT * FROM table", 0.001) < 1


def test_faithfulness_does_not_flatten_matching_dimensions_across_columns():
    rows = [
        {"product_name": "Jeans", "category": "Jeans"},
        {"product_name": "Jeans", "category": "Jeans"},
        {"product_name": "Boots", "category": "Shoes"},
    ]
    report = AnalysisReport(
        question="question",
        answer="Jeans appears in 4 out of 3 returned products.",
    )

    assert _faithfulness_score(report, rows, "SELECT * FROM table", 0.001) < 1


def test_faithfulness_uses_entity_cue_to_disambiguate_numeric_id():
    report = AnalysisReport(question="question", answer="Customer ID 67493.")

    score = _faithfulness_score(
        report,
        [{"customer_id": 67_493, "order_id": 123}],
        "SELECT customer_id, order_id FROM table",
        tolerance=0.001,
    )

    assert score == 1


@pytest.mark.parametrize(
    "answer",
    ["Customer ID 67494.", "Customer ID $67493.", "Customer ID:: 67493."],
)
def test_faithfulness_rejects_wrong_or_typed_numeric_id(answer):
    report = AnalysisReport(question="question", answer=answer)

    score = _faithfulness_score(
        report,
        [{"customer_id": 67_493}],
        "SELECT customer_id FROM table",
        tolerance=0.001,
    )

    assert score == 0


def test_quality_eval_requires_human_usefulness_score(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    replay = case.replay.model_copy(update={"usefulness_score": None})

    case_result = evaluate_quality_case(test_config, case, replay)
    suite = summarize_quality_results("live", [case_result])

    assert case_result.needs_human_review is True
    assert case_result.passed is False
    assert case_result.automated_passed is True
    assert suite.needs_human_review is True
    assert suite.passed is False
    assert suite.automated_passed is True


def test_quality_eval_rejects_degraded_report(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    replay = case.replay.model_copy(
        update={"report": case.replay.report.model_copy(update={"degraded": True})}
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.passed is False


def test_quality_dataset_contains_multi_turn_and_critical_cases():
    cases = load_quality_cases(CASES_PATH)

    assert any(case.history for case in cases)
    assert any(case.critical for case in cases)
    assert all(case.canonical_sql for case in cases)
    assert all(case.suite == "smoke" for case in cases)
    assert all(case.title and case.category for case in cases)
    assert all(case.reference_date == date(2026, 7, 13) for case in cases)


def test_quality_dataset_rejects_duplicate_case_ids(tmp_path):
    case = load_quality_cases(CASES_PATH)[0]
    path = tmp_path / "duplicates.jsonl"
    path.write_text(f"{case.model_dump_json()}\n{case.model_dump_json()}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate evaluation case IDs"):
        load_quality_cases(path)


@pytest.mark.parametrize(
    "contents,error",
    [
        pytest.param("", "dataset is empty", id="empty"),
        pytest.param("not-json\n", "Invalid evaluation case", id="malformed-json"),
    ],
)
def test_quality_dataset_fails_closed(tmp_path, contents, error):
    path = tmp_path / "invalid.jsonl"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        load_quality_cases(path)


def test_quality_dataset_rejects_unknown_fields(tmp_path):
    raw = json.loads(load_quality_cases(CASES_PATH)[0].model_dump_json())
    raw["unexpected"] = True
    path = tmp_path / "unknown-field.jsonl"
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        load_quality_cases(path)


class CanonicalWarehouse:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, str]] = []

    def execute(self, sql: str, trace_id: str):
        from retail_agent.models import QueryResult

        self.calls.append((sql, trace_id))
        return QueryResult(
            sql=sql,
            rows=self.rows,
            total_rows=len(self.rows),
            dry_run_bytes=100,
            total_bytes_billed=80,
            job_id=f"reference-{len(self.calls)}",
            cache_hit=False,
        )


class FailingCanonicalWarehouse(CanonicalWarehouse):
    def execute(self, sql: str, trace_id: str):
        raise RuntimeError("warehouse unavailable")


def _single_case_file(tmp_path, index: int = 0) -> Path:
    raw = CASES_PATH.read_text(encoding="utf-8").splitlines()[index]
    path = tmp_path / "case.jsonl"
    path.write_text(raw + "\n", encoding="utf-8")
    return path


def test_live_quality_eval_compares_agent_and_canonical_results(test_config, tmp_path, monkeypatch):
    case = load_quality_cases(CASES_PATH)[0]
    calls = 0
    chart_executor = object()

    async def fake_run_question(question, *, conversation, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["chart_executor"] is chart_executor
        if calls == 1:
            return TurnResult(
                response=AgentFailure(
                    question=question,
                    message="Temporary outage",
                    failure_code="model_unavailable",
                    retryable=True,
                ),
                conversation=conversation.fail_turn(max_turns=6),
                operational=OperationalMetrics(
                    trace_ids=["attempt-1"],
                    provider_requests=1,
                ),
            )
        return TurnResult(
            response=case.replay.report,
            conversation=conversation.complete_turn(messages=[], max_turns=6),
            retrieved_trio_ids=tuple(case.replay.retrieved_ids),
            query_result=CanonicalWarehouse(case.replay.candidate_rows).execute(
                case.replay.candidate_sql, "trace"
            ),
            operational=case.replay.operational,
        )

    monkeypatch.setattr("evals.quality.run_question", fake_run_question)
    result = asyncio.run(
        run_quality_live_evals(
            test_config,
            _single_case_file(tmp_path),
            bigquery=CanonicalWarehouse(case.replay.canonical_rows),
            golden_store=object(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            analysis_agent=object(),
            chart_executor=chart_executor,
            human_scores={case.id: 5},
            max_safe_attempts=2,
            retry_delay_seconds=0,
        )
    )

    assert result.passed is True
    assert result.mode == "live"
    assert calls == 2
    assert result.results[0].operational.provider_requests == 3
    assert len(result.results[0].operational.trace_ids) == 2
    assert result.results[0].operational.query_attempts == 1


def test_live_quality_eval_uses_fixed_reference_date_and_paces_attempts(
    test_config, tmp_path, monkeypatch
):
    case = load_quality_cases(CASES_PATH)[3]
    reference_dates = []
    analysis_models = []
    sleeps = []

    async def fake_run_question(question, *, conversation, **kwargs):
        reference_dates.append(kwargs["reference_date_override"])
        analysis_models.append(kwargs["analysis_model"])
        return TurnResult(
            response=case.replay.report,
            conversation=conversation.complete_turn(messages=[], max_turns=6),
            retrieved_trio_ids=tuple(case.replay.retrieved_ids),
            query_result=CanonicalWarehouse(case.replay.candidate_rows).execute(
                case.replay.candidate_sql, "trace"
            ),
            reference_date=kwargs["reference_date_override"],
            operational=case.replay.operational,
        )

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("evals.quality.run_question", fake_run_question)
    monkeypatch.setattr("evals.quality.asyncio.sleep", fake_sleep)

    result = asyncio.run(
        run_quality_live_evals(
            test_config,
            _single_case_file(tmp_path, 3),
            bigquery=CanonicalWarehouse(case.replay.canonical_rows),
            golden_store=object(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            analysis_agent=object(),
            analysis_model="concrete-model",
            human_scores={case.id: 5},
            repetitions=2,
            inter_attempt_delay_seconds=0.25,
        )
    )

    assert result.repetitions == 2
    assert reference_dates == [case.reference_date] * 4
    assert analysis_models == ["concrete-model"] * 4
    assert sleeps == [0.25, 0.25, 0.25]


def test_regional_follow_up_result_is_order_independent(test_config):
    case = load_quality_cases(CASES_PATH)[3]
    replay = case.replay.model_copy(
        update={"candidate_rows": list(reversed(case.replay.candidate_rows))}
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.scores.calculation == 1


def test_live_quality_eval_reports_failed_history_turn(test_config, tmp_path, monkeypatch):
    case = load_quality_cases(CASES_PATH)[3]
    calls = 0

    async def fake_run_question(question, *, conversation, **kwargs):
        nonlocal calls
        calls += 1
        return TurnResult(
            response=AgentFailure(
                question=question,
                message="Unavailable",
                failure_code="model_unavailable",
                retryable=True,
            ),
            conversation=conversation.fail_turn(max_turns=6),
            sql_tool_invoked=True,
        )

    monkeypatch.setattr("evals.quality.run_question", fake_run_question)
    result = asyncio.run(
        run_quality_live_evals(
            test_config,
            _single_case_file(tmp_path, 3),
            bigquery=CanonicalWarehouse(case.replay.canonical_rows),
            golden_store=object(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            analysis_agent=object(),
            max_safe_attempts=3,
            retry_delay_seconds=0,
        )
    )

    assert result.passed is False
    assert result.results[0].detail == "history turn failed"
    assert result.results[0].needs_human_review is False
    assert result.automated_passed is False
    assert calls == 1


def test_live_quality_eval_reports_canonical_query_failure(test_config, tmp_path, monkeypatch):
    case = load_quality_cases(CASES_PATH)[0]

    async def fake_run_question(question, *, conversation, **kwargs):
        return TurnResult(
            response=case.replay.report,
            conversation=conversation,
            retrieved_trio_ids=tuple(case.replay.retrieved_ids),
            query_result=CanonicalWarehouse(case.replay.candidate_rows).execute(
                case.replay.candidate_sql, "trace"
            ),
        )

    monkeypatch.setattr("evals.quality.run_question", fake_run_question)
    result = asyncio.run(
        run_quality_live_evals(
            test_config,
            _single_case_file(tmp_path),
            bigquery=FailingCanonicalWarehouse([]),
            golden_store=object(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            analysis_agent=object(),
            max_safe_attempts=1,
        )
    )

    assert result.passed is False
    assert "canonical query failed" in result.results[0].detail


def test_quality_report_and_human_scores_round_trip(test_config, tmp_path):
    from evals.quality import load_human_scores, write_quality_report

    result = run_quality_replay_evals(test_config, CASES_PATH)
    report_path = tmp_path / "artifacts" / "report.json"
    scores_path = tmp_path / "scores.json"
    scores_path.write_text(json.dumps({"case": 4.5}), encoding="utf-8")

    write_quality_report(result, report_path)

    assert json.loads(report_path.read_text(encoding="utf-8"))["passed"] is True
    reloaded = type(result).model_validate_json(report_path.read_text(encoding="utf-8"))
    assert reloaded == result
    assert all(item.versions == result.versions for item in reloaded.results)
    assert load_human_scores(scores_path) == {"case": 4.5}
    assert load_human_scores(None) == {}


def test_row_comparison_accepts_semantically_equivalent_aliases():
    candidate = [{"region": "New York", "lost_revenue_to_returns": 300.0}]
    canonical = [{"region": "New York", "lost_revenue": 300}]

    assert _row_score(candidate, canonical, tolerance=0.001) == 1


def test_result_contract_accepts_unambiguous_semantic_live_aliases():
    case = load_quality_cases(CASES_PATH)[-1]
    candidate = [
        {"state": "California", "lost_revenue_to_returns": 4695.7},
        {"state": "New York", "lost_revenue_to_returns": 1697.71},
    ]
    canonical = [
        {"region": "California", "lost_revenue": 4695.700005531311},
        {"region": "New York", "lost_revenue": 1697.7099933624268},
    ]

    assert (
        _row_score(
            candidate,
            canonical,
            tolerance=case.result_contract.numeric_tolerance,
            contract=case.result_contract,
        )
        == 1
    )


def test_row_comparison_penalizes_additional_candidate_rows():
    candidate = [
        {"region": "New York", "revenue": 300},
        {"region": "Bogus", "revenue": 999_999},
    ]
    canonical = [{"region": "New York", "revenue": 300}]

    assert _row_score(candidate, canonical, tolerance=0.001) == 0.5


def test_row_comparison_allows_additional_candidate_measures():
    candidate = [
        {
            "state": "New York",
            "lost_revenue_to_returns": 300,
            "total_sales": 1_000,
            "return_rate": 0.3,
        }
    ]
    canonical = [{"region": "New York", "lost_revenue": 300}]

    assert _row_score(candidate, canonical, tolerance=0.001) == 1


def test_row_comparison_does_not_match_value_from_unrelated_column():
    candidate = [{"state": "New York", "orders": 300}]
    canonical = [{"region": "New York", "revenue": 300}]

    assert _row_score(candidate, canonical, tolerance=0.001) == 0


def test_faithfulness_accepts_top_n_and_current_date_context():
    current_year = datetime.now(UTC).year
    report = AnalysisReport(
        question="question",
        answer=(f"The top 10 result for calendar year {current_year} produced 100 in revenue."),
    )

    score = _faithfulness_score(
        report,
        [{"revenue": 100}],
        "SELECT revenue FROM table WHERE day <= CURRENT_DATE() LIMIT 10",
        tolerance=0.001,
    )

    assert score == 1


def test_faithfulness_uses_explicit_reference_date_for_current_date_context():
    reference_date = date(2031, 4, 10)
    report = AnalysisReport(
        question="question",
        answer="Revenue was 100 in calendar year 2031.",
    )

    assessment = assess_report_evidence(
        report,
        [{"revenue": 100}],
        "SELECT revenue FROM table WHERE day <= CURRENT_DATE()",
        reference_date=reference_date,
    )

    assert assessment.is_supported


def test_faithfulness_accepts_top_n_before_numeric_identifier_dimension():
    report = AnalysisReport(
        question="question",
        answer="Here are the top 10 customers by spend.",
    )

    score = _faithfulness_score(
        report,
        [{"customer_id": 67_493, "total_spend": 1_549.39}],
        "SELECT customer_id, total_spend FROM table LIMIT 10",
        tolerance=0.001,
    )

    assert score == 1


@pytest.mark.parametrize(
    "answer,sql",
    [
        (
            f"In {datetime.now(UTC).year}, revenue was 100.",
            "SELECT revenue FROM table WHERE day <= CURRENT_DATE()",
        ),
        (
            "Revenue was 100 over the last 3 months.",
            "SELECT revenue FROM table WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 MONTH)",
        ),
        (
            "The query returned 10 results with revenue of 100.",
            "SELECT revenue FROM table LIMIT 10",
        ),
        (
            f"Month {datetime.now(UTC).month} revenue was 100.",
            "SELECT revenue FROM table WHERE day <= CURRENT_DATE()",
        ),
    ],
)
def test_faithfulness_accepts_structurally_bound_context_numbers(answer, sql):
    report = AnalysisReport(question="question", answer=answer)

    score = _faithfulness_score(
        report,
        [{"revenue": 100}],
        sql,
        tolerance=0.001,
    )

    assert score == 1


@pytest.mark.parametrize(
    "answer,rows,sql,expected",
    [
        pytest.param(
            "Revenue was USD10M.",
            [{"revenue": 10_000_000}],
            "SELECT revenue FROM table",
            1,
            id="currency-code-prefix",
        ),
        pytest.param(
            "Revenue was 2026€.",
            [{"revenue": 2_026}],
            "SELECT revenue FROM table WHERE day <= CURRENT_DATE()",
            1,
            id="postfix-currency-symbol",
        ),
        pytest.param(
            "The return rate was in the top 10%.",
            [{"return_rate": 0.1}],
            "SELECT return_rate FROM table LIMIT 10",
            1,
            id="percentage-metric-not-limit",
        ),
        pytest.param(
            "501 Jeans generated 4000 in revenue.",
            [{"product_name": "501 Jeans", "revenue": 4_000}],
            "SELECT product_name, revenue FROM table LIMIT 10",
            1,
            id="numeric-string-dimension",
        ),
        pytest.param(
            "Revenue was 501.",
            [{"product_name": "501 Jeans", "revenue": 4_000}],
            "SELECT product_name, revenue FROM table LIMIT 10",
            0,
            id="no-borrow-from-string-dimension",
        ),
        pytest.param(
            "Revenue was 50.1K and the first category led the second by 5.4%.",
            [
                {"category": "first", "revenue": 50_091.67},
                {"category": "second", "revenue": 47_518.39},
            ],
            "SELECT revenue FROM table LIMIT 10",
            1,
            id="rounded-and-derived-percentage",
        ),
        pytest.param(
            "The result was 105.",
            [{"orders": 100, "customers": 5}],
            "SELECT orders, customers FROM table LIMIT 10",
            0,
            id="no-cross-measure-combination",
        ),
        pytest.param(
            "Revenue was 50.",
            [{"orders": 50, "revenue": 4_000}],
            "SELECT orders, revenue FROM table LIMIT 10",
            0,
            id="named-measure-association",
        ),
        pytest.param(
            "Revenue was 50.",
            [{"revenue": 0.5}],
            "SELECT revenue FROM table LIMIT 10",
            0,
            id="no-implicit-percent-scaling",
        ),
        pytest.param(
            "Revenue was 10.",
            [{"revenue": 4_000}],
            "SELECT revenue FROM table LIMIT 10",
            0,
            id="limit-is-not-measure",
        ),
        pytest.param(
            "Revenue reached $50 from 12 orders.",
            [{"revenue": 50, "orders": 12}],
            "SELECT revenue, orders FROM table LIMIT 10",
            1,
            id="currency-disambiguates-orders",
        ),
        pytest.param(
            "New York lost $300 compared with California.",
            [{"lost_revenue": 300, "total_sales": 1_000}],
            "SELECT lost_revenue, total_sales FROM table LIMIT 10",
            1,
            id="loss-cue-selects-measure",
        ),
        pytest.param(
            "The two categories generated over $97,000 in revenue.",
            [{"revenue": 50_091.67}, {"revenue": 47_518.39}],
            "SELECT revenue FROM table LIMIT 10",
            1,
            id="rounded-derived-total",
        ),
    ],
)
def test_faithfulness_numeric_claim_cases(answer, rows, sql, expected):
    report = AnalysisReport(question="question", answer=answer)

    assert _faithfulness_score(report, rows, sql, tolerance=0.001) == expected


def test_faithfulness_accepts_percentage_rendered_from_share_measure():
    report = AnalysisReport(
        question="question",
        answer="The verified return share was 14.57%.",
    )

    assert _faithfulness_score(
        report,
        [{"return_share": 0.1457}],
        "SELECT return_share FROM table",
        tolerance=0.001,
    ) == 1


def test_faithfulness_accepts_percentage_point_measure_without_rescaling():
    report = AnalysisReport(
        question="question",
        answer="The verified growth rate was 2,661.43%.",
    )

    assert _faithfulness_score(
        report,
        [{"growth_rate_pct": 2661.43}],
        "SELECT growth_rate_pct FROM table",
        tolerance=0.001,
    ) == 1


def test_quality_eval_rejects_unverified_report_sql(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    replay = case.replay.model_copy(
        update={"report": case.replay.report.model_copy(update={"sql": "SELECT 1"})}
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.automated_passed is False
    assert "sql_source=unverified" in result.detail


def test_intent_score_rejects_wrong_join_structure(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    wrong_join = case.replay.candidate_sql.replace("oi.product_id = p.id", "p.id = p.id")

    score = _intent_score(
        test_config,
        wrong_join,
        case.canonical_sql,
        case.expectations,
    )

    assert score == 0


def test_intent_score_rejects_wrong_cross_table_join_key(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    wrong_join = case.replay.candidate_sql.replace("oi.product_id = p.id", "oi.order_id = p.id")

    score = _intent_score(
        test_config,
        wrong_join,
        case.canonical_sql,
        case.expectations,
    )

    assert score == 0


def test_intent_score_accepts_declared_cte_join_key(test_config):
    case = load_quality_cases(Path("evals/datasets/release_holdout.jsonl"))[17]

    score = _intent_score(
        test_config,
        case.replay.candidate_sql,
        case.canonical_sql,
        case.expectations,
    )

    assert case.id == "new_vs_repeat_revenue_mix"
    assert score == 1


def test_intent_score_normalizes_equivalent_quarter_intervals(test_config):
    case = load_quality_cases(CASES_PATH)[3]
    equivalent_sql = case.canonical_sql.replace("INTERVAL 1 QUARTER", "INTERVAL 3 MONTH")

    score = _intent_score(
        test_config,
        equivalent_sql,
        case.canonical_sql,
        case.expectations,
    )

    assert score == 1


def test_intent_signature_normalizes_equivalent_date_casts():
    date_function = _intent_signature("SELECT DATE(created_at) AS day FROM dataset.table")
    date_cast = _intent_signature("SELECT CAST(created_at AS DATE) AS day FROM dataset.table")

    assert date_function.functions == date_cast.functions == frozenset()


def test_intent_score_accepts_equivalent_timestamp_period_boundaries(test_config):
    case = next(
        item
        for item in load_quality_cases(CASES_PATH)
        if item.id == "regional_returns_follow_up"
    )
    equivalent_sql = """
        SELECT
          u.state AS state,
          ROUND(SUM(CASE WHEN oi.status = 'Returned' THEN oi.sale_price ELSE 0 END), 2)
            AS returned_revenue,
          COUNT(DISTINCT CASE WHEN oi.status = 'Returned' THEN oi.order_id END)
            AS returned_orders
        FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
        JOIN `bigquery-public-data.thelook_ecommerce.users` AS u ON oi.user_id = u.id
        WHERE u.state IN ('California', 'New York')
          AND oi.created_at >= CAST(
            DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 QUARTER), QUARTER)
            AS TIMESTAMP
          )
          AND oi.created_at < CAST(DATE_TRUNC(CURRENT_DATE(), QUARTER) AS TIMESTAMP)
        GROUP BY u.state
        ORDER BY returned_revenue DESC
        LIMIT 10
    """

    score = _intent_score(
        test_config,
        equivalent_sql,
        case.canonical_sql,
        case.expectations,
    )

    assert score == 1


def test_intent_score_resolves_reference_date_to_fixed_bounds(test_config):
    canonical = """
        SELECT SUM(sale_price) AS revenue
        FROM `bigquery-public-data.thelook_ecommerce.order_items`
        WHERE DATE(created_at) >= DATE_TRUNC(
          DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH
        )
          AND DATE(created_at) < DATE_TRUNC(CURRENT_DATE(), MONTH)
    """
    candidate = """
        SELECT SUM(sale_price) AS revenue
        FROM `bigquery-public-data.thelook_ecommerce.order_items`
        WHERE DATE(created_at) >= DATE '2026-06-01'
          AND DATE(created_at) < DATE '2026-07-01'
    """

    assessment = _intent_assessment(
        test_config,
        candidate,
        canonical,
        QualityExpectations(),
        reference_date=date(2026, 7, 13),
    )

    assert assessment.score == 1


def test_intent_score_rejects_wrong_resolved_reference_bounds(test_config):
    canonical = """
        SELECT SUM(sale_price) AS revenue
        FROM `bigquery-public-data.thelook_ecommerce.order_items`
        WHERE DATE(created_at) >= DATE_TRUNC(
          DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH
        )
          AND DATE(created_at) < DATE_TRUNC(CURRENT_DATE(), MONTH)
    """
    candidate = canonical.replace("CURRENT_DATE()", "DATE '2026-08-13'")

    assessment = _intent_assessment(
        test_config,
        candidate,
        canonical,
        QualityExpectations(),
        reference_date=date(2026, 7, 13),
    )

    assert assessment.score == 0


def test_intent_signature_allows_additional_aggregates():
    canonical = _intent_signature(
        "SELECT region, SUM(revenue) AS revenue FROM table GROUP BY region"
    )
    candidate = _intent_signature(
        "SELECT region, SUM(revenue) AS revenue, COUNT(*) AS orders FROM table GROUP BY region"
    )

    assert candidate.satisfies(canonical)


def test_intent_signature_allows_additional_grouping_for_singleton_projection():
    canonical = _intent_signature("SELECT SUM(revenue) AS revenue FROM table")
    candidate = _intent_signature(
        "SELECT month, SUM(revenue) AS revenue FROM table GROUP BY month"
    )

    assert candidate.satisfies(canonical)


def test_intent_signature_enforces_explicit_result_limit():
    canonical = _intent_signature(
        "SELECT region, SUM(revenue) AS revenue FROM table GROUP BY region LIMIT 10"
    )
    missing_limit = _intent_signature(
        "SELECT region, SUM(revenue) AS revenue FROM table GROUP BY region"
    )
    wrong_limit = _intent_signature(
        "SELECT region, SUM(revenue) AS revenue FROM table GROUP BY region LIMIT 20"
    )

    assert not missing_limit.satisfies(canonical)
    assert not wrong_limit.satisfies(canonical)


def test_intent_signature_treats_non_distinct_row_counts_as_equivalent():
    count_star = _intent_signature("SELECT COUNT(*) AS rows FROM table")
    count_id = _intent_signature("SELECT COUNT(id) AS rows FROM table")
    distinct_orders = _intent_signature("SELECT COUNT(DISTINCT order_id) AS rows FROM table")

    assert count_id.satisfies(count_star)
    assert count_star.satisfies(count_id)
    assert not count_star.satisfies(distinct_orders)


def test_intent_signature_normalizes_equivalent_conditional_counts():
    count_if = _intent_signature(
        "SELECT COUNTIF(status = 'Returned') AS returned_items FROM table"
    )
    sum_case = _intent_signature(
        "SELECT SUM(CASE WHEN status = 'Returned' THEN 1 ELSE 0 END) AS returned_items "
        "FROM table"
    )

    assert count_if.satisfies(sum_case)
    assert sum_case.satisfies(count_if)


def test_row_score_accepts_equivalent_month_labels():
    contract = ResultContract(
        key_columns=["month"],
        measure_columns=["revenue"],
        column_mapping={"month": "month", "revenue": "revenue"},
        units={"month": "date", "revenue": "currency"},
    )

    assert _row_score(
        [{"month": "2026-05", "revenue": 10.0}],
        [{"month": "2026-05-01", "revenue": 10.0}],
        0.001,
        contract,
    ) == 1


def test_row_score_accepts_formatted_month_against_date_value():
    contract = ResultContract(
        key_columns=["month"],
        measure_columns=["revenue"],
        column_mapping={"month": "month", "revenue": "revenue"},
        units={"month": "date", "revenue": "currency"},
    )

    assert _row_score(
        [{"month": "2026-05", "revenue": 10.0}],
        [{"month": date(2026, 5, 1), "revenue": 10.0}],
        0.001,
        contract,
    ) == 1


def test_row_score_accepts_month_name_against_fixed_date_value():
    contract = ResultContract(
        key_columns=["month"],
        measure_columns=["revenue"],
        column_mapping={"month": "month", "revenue": "revenue"},
        units={"month": "date", "revenue": "currency"},
    )

    assert _row_score(
        [{"month": "May", "revenue": 10.0}],
        [{"month": date(2026, 5, 1), "revenue": 10.0}],
        0.001,
        contract,
    ) == 1


def test_row_score_normalizes_percentage_scale_and_alias():
    contract = ResultContract(
        key_columns=["state"],
        measure_columns=["revenue_share"],
        column_mapping={"state": "state", "revenue_share": "revenue_share"},
        units={"state": "text", "revenue_share": "percentage"},
    )

    assert _row_score(
        [{"state": "California", "revenue_percentage": 5.4768}],
        [{"state": "California", "revenue_share": 0.055}],
        0.001,
        contract,
    ) == 1


def test_row_score_normalizes_pct_scale_and_alias():
    contract = ResultContract(
        key_columns=["state"],
        measure_columns=["growth_rate"],
        column_mapping={"state": "state", "growth_rate": "growth_rate"},
        units={"state": "text", "growth_rate": "percentage"},
    )

    assert _row_score(
        [{"state": "A", "growth_rate_pct": 2661.43}],
        [{"state": "A", "growth_rate": 26.614}],
        0.001,
        contract,
    ) == 1


def test_row_score_normalizes_cohort_labels_and_measure_aliases():
    contract = ResultContract(
        key_columns=["cohort"],
        measure_columns=["average_spend"],
        column_mapping={"cohort": "cohort", "average_spend": "average_spend"},
        units={"cohort": "text", "average_spend": "currency"},
    )

    assert _row_score(
        [
            {"customer_segment": "One-order customers", "average_customer_spend": 42.0},
            {"customer_segment": "More than one order", "average_customer_spend": 84.0},
        ],
        [
            {"cohort": "single_order", "average_spend": 42.0},
            {"cohort": "repeat", "average_spend": 84.0},
        ],
        0.001,
        contract,
    ) == 1


def test_intent_signature_ignores_cosmetic_rounding():
    canonical = _intent_signature(
        "SELECT region, ROUND(SUM(revenue), 2) AS revenue FROM table GROUP BY region"
    )
    candidate = _intent_signature(
        "SELECT region, SUM(revenue) AS revenue FROM table GROUP BY region"
    )

    assert candidate.satisfies(canonical)


def test_intent_signature_preserves_grouped_rounding_semantics():
    canonical = _intent_signature(
        "SELECT ROUND(price, 2) AS bucket, COUNT(*) FROM table GROUP BY bucket"
    )
    raw_candidate = _intent_signature("SELECT price AS bucket, COUNT(*) FROM table GROUP BY bucket")
    wrong_precision = _intent_signature(
        "SELECT ROUND(price, 1) AS bucket, COUNT(*) FROM table GROUP BY bucket"
    )

    assert not raw_candidate.satisfies(canonical)
    assert not wrong_precision.satisfies(canonical)


def test_intent_signature_preserves_rounding_used_by_having():
    canonical = _intent_signature(
        "SELECT region, ROUND(SUM(revenue), 2) AS revenue FROM table "
        "GROUP BY region HAVING revenue > 10"
    )
    candidate = _intent_signature(
        "SELECT region, SUM(revenue) AS revenue FROM table GROUP BY region HAVING revenue > 10"
    )

    assert not candidate.satisfies(canonical)


def test_retrieval_scores_include_recall_at_three_and_mrr():
    recall, mrr = _retrieval_scores(["other", "expected"], ["expected"])

    assert recall == 1
    assert mrr == 0.5


def test_retrieval_assessment_separates_relevance_utility_and_harm():
    case = load_quality_cases(CASES_PATH)[0]
    contract = RetrievalContract(
        relevant_ids=["relevant"],
        acceptable_ids=["acceptable"],
        forbidden_ids=["distractor"],
        useful_sql_fragments=["group by"],
        harmful_sql_fragments=["select email"],
    )
    replay = case.replay.model_copy(
        update={"retrieved_ids": ["acceptable", "distractor", "relevant"]}
    )

    assessment = _retrieval_assessment(replay, contract)

    assert assessment.recall_at_three == 1
    assert assessment.mrr == pytest.approx(1 / 3)
    assert assessment.ndcg_at_three == pytest.approx(0.68852888)
    assert assessment.irrelevant_rate == pytest.approx(1 / 3)
    assert assessment.usefulness == 1
    assert assessment.harm_rate == 0


def test_retrieval_harm_detects_distractor_constraint_in_candidate_sql():
    case = load_quality_cases(CASES_PATH)[0]
    replay = case.replay.model_copy(
        update={"candidate_sql": f"{case.replay.candidate_sql} SELECT email"}
    )

    assessment = _retrieval_assessment(
        replay,
        RetrievalContract(harmful_sql_fragments=["select email"]),
    )

    assert assessment.harm_rate == 1


def test_answer_contract_rejects_pii_in_narrative(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    replay = case.replay.model_copy(
        update={
            "report": case.replay.report.model_copy(
                update={"answer": "Contact jane@example.com for the result."}
            )
        }
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.scores.faithfulness == 0
    assert "pii_leakage:1" in result.diagnostics.unsupported_qualitative_claims


@pytest.mark.parametrize(
    ("update", "failed_constraint"),
    [
        ({"provider_requests": 99}, "provider_request_budget"),
        ({"duplicate_warehouse_executions": 1}, "duplicate_warehouse_execution"),
        ({"tool_order_compliant": False}, "tool_order"),
        ({"billed_bytes": 50_000_001}, "billed_bytes_budget"),
    ],
)
def test_operational_budget_mutations_fail_only_operational_score(
    test_config, update, failed_constraint
):
    case = load_quality_cases(CASES_PATH)[0]
    baseline = evaluate_quality_case(test_config, case, case.replay)
    replay = case.replay.model_copy(
        update={"operational": case.replay.operational.model_copy(update=update)}
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert baseline.scores.operational == 1
    assert result.scores.operational == 0
    assert {
        name
        for name in type(result.scores).model_fields
        if getattr(result.scores, name) != getattr(baseline.scores, name)
    } == {"operational"}
    assert any(
        item.name == failed_constraint and not item.passed
        for item in result.diagnostics.operational_results
    )


def test_provider_latency_is_observed_but_does_not_fail_release_gate(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    replay = case.replay.model_copy(
        update={
            "operational": case.replay.operational.model_copy(
                update={"duration_ms": 120_000}
            )
        }
    )

    result = evaluate_quality_case(test_config, case, replay)

    duration = next(
        item
        for item in result.diagnostics.operational_results
        if item.name == "duration_observation"
    )
    assert duration.passed is True
    assert "informational=true" in duration.detail
    assert result.scores.operational == 1


def test_repeated_live_eval_reports_flakiness_and_attempt_statistics(
    test_config, tmp_path, monkeypatch
):
    case = load_quality_cases(CASES_PATH)[0]
    calls = 0

    async def fake_run_question(question, *, conversation, **kwargs):
        nonlocal calls
        calls += 1
        report = case.replay.report
        if calls == 2:
            report = report.model_copy(update={"answer": "Revenue was 99999999."})
        return TurnResult(
            response=report,
            conversation=conversation,
            retrieved_trio_ids=tuple(case.replay.retrieved_ids),
            query_result=CanonicalWarehouse(case.replay.candidate_rows).execute(
                case.replay.candidate_sql, f"trace-{calls}"
            ),
            operational=case.replay.operational.model_copy(
                update={
                    "trace_ids": [f"trace-{calls}"],
                    "duration_ms": calls * 100,
                    "bigquery_job_ids": [f"job-{calls}"],
                }
            ),
        )

    monkeypatch.setattr("evals.quality.run_question", fake_run_question)
    warehouse = CanonicalWarehouse(case.replay.canonical_rows)
    result = asyncio.run(
        run_quality_live_evals(
            test_config,
            _single_case_file(tmp_path),
            bigquery=warehouse,
            golden_store=object(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            analysis_agent=object(),
            human_scores={case.id: 5},
            max_safe_attempts=1,
            repetitions=5,
        )
    )

    assert calls == 5
    assert len(warehouse.calls) == 1
    assert result.case_count == 1
    assert result.attempt_count == 5
    assert result.repetitions == 5
    assert result.flaky_cases == [case.id]
    assert result.stability[case.id].successes == 4
    assert result.stability[case.id].first_attempt_passed is True
    assert result.stability[case.id].eventual_passed is True
    assert result.stability[case.id].worst_scores["faithfulness"] == 0
    assert result.stability[case.id].score_standard_deviation["faithfulness"] > 0
    assert result.operational.first_attempt_success_rate == 1
    assert result.operational.eventual_success_rate == 1
    assert result.operational.attempt_success_rate == pytest.approx(4 / 5)
    assert result.operational.p50_duration_ms == 300
    assert result.operational.p95_duration_ms == pytest.approx(480)
    assert result.operational.pass_rate_ci95 is not None
    assert result.reference_queries.attempts == 1
    assert result.reference_queries.executions == 1
    assert result.reference_queries.dry_run_bytes == 100
    assert result.reference_queries.billed_bytes == 80


def test_live_multi_turn_eval_merges_history_and_final_operational_metrics(
    test_config, tmp_path, monkeypatch
):
    case = load_quality_cases(CASES_PATH)[3]
    calls = 0

    async def fake_run_question(question, *, conversation, **kwargs):
        nonlocal calls
        calls += 1
        return TurnResult(
            response=case.replay.report,
            conversation=conversation.complete_turn(
                messages=[ModelRequest(parts=[UserPromptPart(content=question)])],
                max_turns=6,
            ),
            retrieved_trio_ids=tuple(case.replay.retrieved_ids),
            query_result=CanonicalWarehouse(case.replay.candidate_rows).execute(
                case.replay.candidate_sql, f"trace-{calls}"
            ),
            operational=case.replay.operational.model_copy(
                update={
                    "trace_ids": [f"trace-{calls}"],
                    "provider_requests": 1,
                    "query_attempts": 1,
                    "bigquery_dry_runs": 1,
                    "bigquery_executions": 1,
                    "bigquery_job_ids": [f"job-{calls}"],
                    "dry_run_bytes": 100,
                    "billed_bytes": 80,
                    "total_tokens": 100,
                }
            ),
        )

    monkeypatch.setattr("evals.quality.run_question", fake_run_question)
    result = asyncio.run(
        run_quality_live_evals(
            test_config,
            _single_case_file(tmp_path, 3),
            bigquery=CanonicalWarehouse(case.replay.canonical_rows),
            golden_store=object(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            analysis_agent=object(),
            human_scores={case.id: 5},
            max_safe_attempts=1,
        )
    )

    operations = result.results[0].operational
    assert calls == 2
    assert operations.trace_ids == ["trace-1", "trace-2"]
    assert operations.turn_durations_ms == [1200, 1200]
    assert operations.provider_requests == 2
    assert operations.query_attempts == 2
    assert operations.bigquery_executions == 2
    assert operations.bigquery_job_ids == ["job-1", "job-2"]
    assert operations.total_tokens == 200
    assert result.results[0].automated_passed is True, result.results[0].model_dump_json(indent=2)
