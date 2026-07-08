from retail_agent.evals import run_guardrail_evals


def test_guardrail_evals_pass(test_config):
    results = run_guardrail_evals(test_config)

    assert results
    assert all(result.passed for result in results), results
