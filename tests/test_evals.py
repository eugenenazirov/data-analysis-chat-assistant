from pydantic_evals import Dataset

from evals.guardrails import build_guardrail_dataset, run_guardrail_evals


def test_guardrail_evals_pass(test_config):
    results = run_guardrail_evals(test_config)

    assert results
    assert all(result.passed for result in results), results


def test_guardrail_dataset_covers_security_regressions():
    dataset = build_guardrail_dataset()
    names = {case.name for case in dataset.cases}

    assert isinstance(dataset, Dataset)
    assert {
        "table_scope_blocked",
        "malformed_sql_retryable",
        "user_pii_sql_blocked",
    } <= names
