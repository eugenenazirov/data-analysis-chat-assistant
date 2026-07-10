import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from retail_agent.agent import TurnResult
from retail_agent.models import AgentFailure, AnalysisReport
from retail_agent.observability import EventLogger
from retail_agent.quality_evals import (
    _faithfulness_score,
    _intent_score,
    _intent_signature,
    _retrieval_scores,
    _row_score,
    evaluate_quality_case,
    load_quality_cases,
    run_quality_live_evals,
    run_quality_replay_evals,
    summarize_quality_results,
)

CASES_PATH = Path("data/quality_eval_cases.jsonl")


def test_quality_replay_suite_meets_release_gates(test_config):
    result = run_quality_replay_evals(test_config, CASES_PATH)

    assert result.passed is True
    assert result.aggregate.intent == 1
    assert result.aggregate.calculation == 1
    assert result.aggregate.retrieval == 1
    assert result.aggregate.retrieval_mrr == 1
    assert result.aggregate.faithfulness == 1
    assert result.aggregate.multi_turn == 1
    assert result.aggregate.usefulness is not None
    assert result.aggregate.usefulness >= 0.8


def test_quality_eval_rejects_unsupported_numeric_claim(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    replay = case.replay.model_copy(
        update={
            "report": case.replay.report.model_copy(
                update={"answer": "Revenue was 99999999."}
            )
        }
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.passed is False
    assert result.scores.faithfulness == 0


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
def test_quality_eval_rejects_unit_incompatible_numeric_claim(
    test_config, answer, rows
):
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
    case = load_quality_cases(CASES_PATH)[0]
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


@pytest.mark.parametrize("marker", ["~", "≈"])
def test_quality_eval_accepts_symbol_rounded_live_regional_report(
    test_config, marker
):
    case = load_quality_cases(CASES_PATH)[-1]
    rows = [
        {"region": "California", "lost_revenue": 5_225.15},
        {"region": "New York", "lost_revenue": 2_774.17},
    ]
    report = case.replay.report.model_copy(
        update={
            "answer": "California lost $5,225.15 versus New York's $2,774.17.",
            "highlights": [
                f"California's $5,225.15 loss was {marker}1.9x New York's."
            ],
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
def test_quality_eval_accepts_structurally_bound_live_customer_id(
    test_config, identifier_text
):
    case = load_quality_cases(CASES_PATH)[1]
    rows = [{"customer_id": 67_493, "orders": 2, "total_spend": 1_549.39}]
    report = case.replay.report.model_copy(
        update={
            "answer": "Here is our top spending customer:",
            "highlights": [
                f"Our top spending {identifier_text} spent $1549.39 across 2 orders."
            ],
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


class CanonicalWarehouse:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql: str, trace_id: str):
        from retail_agent.models import QueryResult

        return QueryResult(sql=sql, rows=self.rows, total_rows=len(self.rows))


class FailingCanonicalWarehouse(CanonicalWarehouse):
    def execute(self, sql: str, trace_id: str):
        raise RuntimeError("warehouse unavailable")


def _single_case_file(tmp_path, index: int = 0) -> Path:
    raw = CASES_PATH.read_text(encoding="utf-8").splitlines()[index]
    path = tmp_path / "case.jsonl"
    path.write_text(raw + "\n", encoding="utf-8")
    return path


def test_live_quality_eval_compares_agent_and_canonical_results(
    test_config, tmp_path, monkeypatch
):
    case = load_quality_cases(CASES_PATH)[0]
    calls = 0

    async def fake_run_question(question, *, conversation, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return TurnResult(
                response=AgentFailure(
                    question=question,
                    message="Temporary outage",
                    failure_code="model_unavailable",
                    retryable=True,
                ),
                conversation=conversation.fail_turn(question=question, max_turns=6),
            )
        return TurnResult(
            response=case.replay.report,
            conversation=conversation.complete_turn(
                question=question, messages=[], max_turns=6
            ),
            retrieved_trio_ids=tuple(case.replay.retrieved_ids),
            query_result=CanonicalWarehouse(case.replay.candidate_rows).execute(
                case.replay.candidate_sql, "trace"
            ),
        )

    monkeypatch.setattr("retail_agent.quality_evals.run_question", fake_run_question)
    result = asyncio.run(
        run_quality_live_evals(
            test_config,
            _single_case_file(tmp_path),
            bigquery=CanonicalWarehouse(case.replay.canonical_rows),
            golden_store=object(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            analysis_agent=object(),
            human_scores={case.id: 5},
            max_safe_attempts=2,
            retry_delay_seconds=0,
        )
    )

    assert result.passed is True
    assert result.mode == "live"
    assert calls == 2


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
            conversation=conversation.fail_turn(question=question, max_turns=6),
            sql_tool_invoked=True,
        )

    monkeypatch.setattr("retail_agent.quality_evals.run_question", fake_run_question)
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


def test_live_quality_eval_reports_canonical_query_failure(
    test_config, tmp_path, monkeypatch
):
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

    monkeypatch.setattr("retail_agent.quality_evals.run_question", fake_run_question)
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
    from retail_agent.quality_evals import load_human_scores, write_quality_report

    result = run_quality_replay_evals(test_config, CASES_PATH)
    report_path = tmp_path / "artifacts" / "report.json"
    scores_path = tmp_path / "scores.json"
    scores_path.write_text(json.dumps({"case": 4.5}), encoding="utf-8")

    write_quality_report(result, report_path)

    assert json.loads(report_path.read_text(encoding="utf-8"))["passed"] is True
    assert load_human_scores(scores_path) == {"case": 4.5}
    assert load_human_scores(None) == {}


def test_row_comparison_accepts_semantically_equivalent_aliases():
    candidate = [{"region": "New York", "lost_revenue_to_returns": 300.0}]
    canonical = [{"region": "New York", "lost_revenue": 300}]

    assert _row_score(candidate, canonical, tolerance=0.001) == 1


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
        answer=(
            f"The top 10 result for calendar year {current_year} "
            "produced 100 in revenue."
        ),
    )

    score = _faithfulness_score(
        report,
        [{"revenue": 100}],
        "SELECT revenue FROM table WHERE day <= CURRENT_DATE() LIMIT 10",
        tolerance=0.001,
    )

    assert score == 1


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
            "SELECT revenue FROM table WHERE day >= "
            "DATE_SUB(CURRENT_DATE(), INTERVAL 3 MONTH)",
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


def test_faithfulness_accepts_supported_currency_code_prefix():
    report = AnalysisReport(
        question="question",
        answer="Revenue was USD10M.",
    )

    score = _faithfulness_score(
        report,
        [{"revenue": 10_000_000}],
        "SELECT revenue FROM table",
        tolerance=0.001,
    )

    assert score == 1


def test_faithfulness_accepts_supported_postfix_currency_symbol():
    report = AnalysisReport(
        question="question",
        answer="Revenue was 2026€.",
    )

    score = _faithfulness_score(
        report,
        [{"revenue": 2_026}],
        "SELECT revenue FROM table WHERE day <= CURRENT_DATE()",
        tolerance=0.001,
    )

    assert score == 1


def test_faithfulness_validates_percentage_against_metric_not_limit():
    report = AnalysisReport(
        question="question",
        answer="The return rate was in the top 10%.",
    )

    score = _faithfulness_score(
        report,
        [{"return_rate": 0.1}],
        "SELECT return_rate FROM table LIMIT 10",
        tolerance=0.001,
    )

    assert score == 1


def test_faithfulness_accepts_number_embedded_in_returned_dimension():
    report = AnalysisReport(
        question="question",
        answer="501 Jeans generated 4000 in revenue.",
    )

    score = _faithfulness_score(
        report,
        [{"product_name": "501 Jeans", "revenue": 4_000}],
        "SELECT product_name, revenue FROM table LIMIT 10",
        tolerance=0.001,
    )

    assert score == 1


def test_faithfulness_does_not_borrow_number_from_string_dimension():
    report = AnalysisReport(
        question="question",
        answer="Revenue was 501.",
    )

    score = _faithfulness_score(
        report,
        [{"product_name": "501 Jeans", "revenue": 4_000}],
        "SELECT product_name, revenue FROM table LIMIT 10",
        tolerance=0.001,
    )

    assert score == 0


def test_faithfulness_accepts_rounded_suffixes_and_derived_percentages():
    report = AnalysisReport(
        question="question",
        answer="Revenue was 50.1K and the first category led the second by 5.4%.",
    )

    score = _faithfulness_score(
        report,
        [
            {"category": "first", "revenue": 50_091.67},
            {"category": "second", "revenue": 47_518.39},
        ],
        "SELECT revenue FROM table LIMIT 10",
        tolerance=0.001,
    )

    assert score == 1


def test_faithfulness_does_not_combine_unrelated_measures():
    report = AnalysisReport(
        question="question",
        answer="The result was 105.",
    )

    score = _faithfulness_score(
        report,
        [{"orders": 100, "customers": 5}],
        "SELECT orders, customers FROM table LIMIT 10",
        tolerance=0.001,
    )

    assert score == 0


def test_faithfulness_associates_numeric_claim_with_named_measure():
    report = AnalysisReport(
        question="question",
        answer="Revenue was 50.",
    )

    score = _faithfulness_score(
        report,
        [{"orders": 50, "revenue": 4_000}],
        "SELECT orders, revenue FROM table LIMIT 10",
        tolerance=0.001,
    )

    assert score == 0


def test_faithfulness_does_not_apply_percent_scaling_without_percent_sign():
    report = AnalysisReport(
        question="question",
        answer="Revenue was 50.",
    )

    score = _faithfulness_score(
        report,
        [{"revenue": 0.5}],
        "SELECT revenue FROM table LIMIT 10",
        tolerance=0.001,
    )

    assert score == 0


def test_faithfulness_does_not_treat_limit_as_a_measure_value():
    report = AnalysisReport(
        question="question",
        answer="Revenue was 10.",
    )

    score = _faithfulness_score(
        report,
        [{"revenue": 4_000}],
        "SELECT revenue FROM table LIMIT 10",
        tolerance=0.001,
    )

    assert score == 0


def test_faithfulness_uses_currency_to_disambiguate_nearby_order_count():
    report = AnalysisReport(
        question="question",
        answer="Revenue reached $50 from 12 orders.",
    )

    score = _faithfulness_score(
        report,
        [{"revenue": 50, "orders": 12}],
        "SELECT revenue, orders FROM table LIMIT 10",
        tolerance=0.001,
    )

    assert score == 1


def test_faithfulness_uses_loss_word_for_currency_measure():
    report = AnalysisReport(
        question="question",
        answer="New York lost $300 compared with California.",
    )

    score = _faithfulness_score(
        report,
        [{"lost_revenue": 300, "total_sales": 1_000}],
        "SELECT lost_revenue, total_sales FROM table LIMIT 10",
        tolerance=0.001,
    )

    assert score == 1


def test_faithfulness_accepts_explicitly_rounded_derived_total():
    report = AnalysisReport(
        question="question",
        answer="The two categories generated over $97,000 in revenue.",
    )

    score = _faithfulness_score(
        report,
        [{"revenue": 50_091.67}, {"revenue": 47_518.39}],
        "SELECT revenue FROM table LIMIT 10",
        tolerance=0.001,
    )

    assert score == 1


def test_quality_eval_rejects_unverified_report_sql(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    replay = case.replay.model_copy(
        update={
            "report": case.replay.report.model_copy(update={"sql": "SELECT 1"})
        }
    )

    result = evaluate_quality_case(test_config, case, replay)

    assert result.automated_passed is False
    assert "sql_source=unverified" in result.detail


def test_intent_score_rejects_wrong_join_structure(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    wrong_join = case.replay.candidate_sql.replace(
        "oi.product_id = p.id", "p.id = p.id"
    )

    score = _intent_score(
        test_config,
        wrong_join,
        case.canonical_sql,
        case.expectations,
    )

    assert score == 0


def test_intent_score_rejects_wrong_cross_table_join_key(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    wrong_join = case.replay.candidate_sql.replace(
        "oi.product_id = p.id", "oi.order_id = p.id"
    )

    score = _intent_score(
        test_config,
        wrong_join,
        case.canonical_sql,
        case.expectations,
    )

    assert score == 0


def test_intent_score_normalizes_equivalent_quarter_intervals(test_config):
    case = load_quality_cases(CASES_PATH)[3]
    equivalent_sql = case.canonical_sql.replace(
        "INTERVAL 1 QUARTER", "INTERVAL 3 MONTH"
    )

    score = _intent_score(
        test_config,
        equivalent_sql,
        case.canonical_sql,
        case.expectations,
    )

    assert score == 1


def test_intent_signature_normalizes_equivalent_date_casts():
    date_function = _intent_signature(
        "SELECT DATE(created_at) AS day FROM dataset.table"
    )
    date_cast = _intent_signature(
        "SELECT CAST(created_at AS DATE) AS day FROM dataset.table"
    )

    assert date_function.functions == date_cast.functions == frozenset({"to_date"})


def test_intent_signature_allows_additional_aggregates():
    canonical = _intent_signature(
        "SELECT region, SUM(revenue) AS revenue FROM table GROUP BY region"
    )
    candidate = _intent_signature(
        "SELECT region, SUM(revenue) AS revenue, COUNT(*) AS orders "
        "FROM table GROUP BY region"
    )

    assert candidate.satisfies(canonical)


def test_retrieval_scores_include_recall_at_three_and_mrr():
    recall, mrr = _retrieval_scores(["other", "expected"], ["expected"])

    assert recall == 1
    assert mrr == 0.5
