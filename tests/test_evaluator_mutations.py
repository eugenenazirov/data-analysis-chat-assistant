import re
from pathlib import Path

import pytest

from evals.quality import (
    ResultContract,
    _row_score,
    evaluate_quality_case,
    load_quality_cases,
    summarize_quality_results,
)

CASES_PATH = Path("evals/datasets/smoke.jsonl")


def _changed_scores(baseline, mutated) -> set[str]:
    return {
        field
        for field in type(baseline.scores).model_fields
        if getattr(baseline.scores, field) != getattr(mutated.scores, field)
    }


def _neutral_replay(case):
    return case.replay.model_copy(
        update={
            "report": case.replay.report.model_copy(
                update={
                    "answer": "The verified result is attached.",
                    "highlights": [],
                    "assumptions": [],
                    "caveats": [],
                    "followups": [],
                }
            )
        }
    )


@pytest.mark.parametrize(
    "mutation,failed_constraint",
    [
        pytest.param(
            lambda sql: sql.replace(
                "`bigquery-public-data.thelook_ecommerce.products`",
                "`bigquery-public-data.thelook_ecommerce.orders`",
            ),
            "safe_sql",
            id="required-table",
        ),
        pytest.param(
            lambda sql: sql.replace("oi.product_id = p.id", "oi.order_id = p.id"),
            "allowed_joins",
            id="join-key",
        ),
        pytest.param(
            lambda sql: sql.replace(
                " AND DATE_TRUNC(DATE(oi.created_at), MONTH) = "
                "DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH)",
                "",
            ),
            "semantic_structure",
            id="date-boundary",
        ),
        pytest.param(
            lambda sql: sql.replace("COUNT(DISTINCT oi.order_id)", "COUNT(*)"),
            "semantic_structure",
            id="distinct-order-count",
        ),
        pytest.param(
            lambda sql: sql.replace("'Returned'", "'Shipped'"),
            "semantic_structure",
            id="returned-item-policy",
        ),
    ],
)
def test_sql_mutations_only_lower_intent_metric(test_config, mutation, failed_constraint):
    case = load_quality_cases(CASES_PATH)[0]
    baseline = evaluate_quality_case(test_config, case, case.replay)
    replay = case.replay.model_copy(update={"candidate_sql": mutation(case.replay.candidate_sql)})

    mutated = evaluate_quality_case(test_config, case, replay)

    assert mutated.scores.intent == 0
    expected_changed = {"intent"}
    if failed_constraint == "semantic_structure" and any(
        fragment.casefold() not in replay.candidate_sql.casefold()
        for fragment in case.retrieval.useful_sql_fragments
    ):
        expected_changed.add("retrieval_usefulness")
    assert _changed_scores(baseline, mutated) == expected_changed
    assert any(
        item.name == failed_constraint and not item.passed
        for item in mutated.diagnostics.constraint_results
    )


@pytest.mark.parametrize(
    "mutate_rows",
    [
        pytest.param(lambda rows: [*rows, {**rows[0], "category": "Extra"}], id="extra-row"),
        pytest.param(lambda rows: rows[:-1], id="missing-row"),
        pytest.param(
            lambda rows: [
                {**row, "revenue": row["orders"], "orders": row["revenue"]} for row in rows
            ],
            id="swapped-measures",
        ),
        pytest.param(
            lambda rows: [
                {"segment": row["category"], **{k: v for k, v in row.items() if k != "category"}}
                for row in rows
            ],
            id="unmapped-field-name",
        ),
    ],
)
def test_row_mutations_only_lower_calculation_metric(test_config, mutate_rows):
    case = load_quality_cases(CASES_PATH)[0]
    replay = _neutral_replay(case)
    baseline = evaluate_quality_case(test_config, case, replay)
    mutated_replay = replay.model_copy(
        update={"candidate_rows": mutate_rows(replay.candidate_rows)}
    )

    mutated = evaluate_quality_case(test_config, case, mutated_replay)

    assert mutated.scores.calculation < baseline.scores.calculation
    assert _changed_scores(baseline, mutated) == {"calculation"}
    assert mutated.diagnostics.result_contract_violations


@pytest.mark.parametrize(
    "rank,expected_changed",
    [
        pytest.param(2, {"retrieval_mrr", "retrieval_ndcg"}, id="rank-2"),
        pytest.param(3, {"retrieval_mrr", "retrieval_ndcg"}, id="rank-3"),
        pytest.param(
            4,
            {"retrieval", "retrieval_mrr", "retrieval_ndcg"},
            id="rank-4",
        ),
    ],
)
def test_retrieval_rank_mutations_only_lower_retrieval_metrics(test_config, rank, expected_changed):
    case = load_quality_cases(CASES_PATH)[0]
    baseline = evaluate_quality_case(test_config, case, case.replay)
    relevant = case.retrieval.relevant_ids[0]
    retrieved = [f"irrelevant_{index}" for index in range(1, rank)] + [relevant]
    replay = case.replay.model_copy(update={"retrieved_ids": retrieved})

    mutated = evaluate_quality_case(test_config, case, replay)

    assert _changed_scores(baseline, mutated) == expected_changed


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param("Revenue was 99999999.", id="unsupported-number"),
        pytest.param("There were $50,091.67 orders.", id="wrong-unit"),
        pytest.param("The result was caused by product quality.", id="unsupported-causal-claim"),
    ],
)
def test_narrative_mutations_only_lower_faithfulness_metric(test_config, answer):
    case = load_quality_cases(CASES_PATH)[0]
    baseline = evaluate_quality_case(test_config, case, case.replay)
    replay = case.replay.model_copy(
        update={"report": case.replay.report.model_copy(update={"answer": answer})}
    )

    mutated = evaluate_quality_case(test_config, case, replay)

    assert mutated.scores.faithfulness == 0
    assert _changed_scores(baseline, mutated) == {"faithfulness"}


def test_different_attached_sql_fails_gate_without_hiding_in_score(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    baseline = evaluate_quality_case(test_config, case, case.replay)
    replay = case.replay.model_copy(
        update={"report": case.replay.report.model_copy(update={"sql": "SELECT 1"})}
    )

    mutated = evaluate_quality_case(test_config, case, replay)

    assert _changed_scores(baseline, mutated) == set()
    assert mutated.automated_passed is False
    assert mutated.diagnostics.verified_sql_attached is False


def test_failed_history_use_only_lowers_multi_turn_metric(test_config):
    case = load_quality_cases(CASES_PATH)[-1]
    baseline = evaluate_quality_case(test_config, case, case.replay)
    replay = case.replay.model_copy(update={"history_used": False})

    mutated = evaluate_quality_case(test_config, case, replay)

    assert _changed_scores(baseline, mutated) == {"multi_turn"}
    assert mutated.scores.multi_turn == 0


def test_missing_human_score_only_marks_usefulness_pending(test_config):
    case = load_quality_cases(CASES_PATH)[0]
    baseline = evaluate_quality_case(test_config, case, case.replay)
    replay = case.replay.model_copy(update={"usefulness_score": None})

    mutated = evaluate_quality_case(test_config, case, replay)

    assert _changed_scores(baseline, mutated) == {"usefulness"}
    assert mutated.automated_passed is True
    assert mutated.needs_human_review is True
    assert mutated.passed is False


def test_critical_failure_cannot_be_hidden_by_averaging(test_config):
    cases = load_quality_cases(CASES_PATH)
    passed = [evaluate_quality_case(test_config, case, case.replay) for case in cases]
    critical = cases[0]
    bad_sql = re.sub(r"COUNT\(DISTINCT oi\.order_id\)", "COUNT(*)", critical.replay.candidate_sql)
    failed = evaluate_quality_case(
        test_config,
        critical,
        critical.replay.model_copy(update={"candidate_sql": bad_sql}),
    )

    suite = summarize_quality_results("replay", [*passed, *passed, failed])

    assert suite.aggregate.intent > 0.8
    assert suite.automated_passed is False
    assert suite.critical_failures == [critical.id]


def test_explicit_column_mapping_accepts_only_declared_alias():
    contract = ResultContract(
        key_columns=["region"],
        measure_columns=["lost_revenue"],
        column_mapping={"region": "state", "lost_revenue": "return_loss"},
        units={"region": "text", "lost_revenue": "currency"},
    )

    assert (
        _row_score(
            [{"state": "New York", "return_loss": 300}],
            [{"region": "New York", "lost_revenue": 300}],
            tolerance=0.001,
            contract=contract,
        )
        == 1
    )
    assert (
        _row_score(
            [{"area": "New York", "return_loss": 300}],
            [{"region": "New York", "lost_revenue": 300}],
            tolerance=0.001,
            contract=contract,
        )
        == 0
    )
