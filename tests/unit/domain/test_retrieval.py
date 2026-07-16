from retail_agent.domain.policies.retrieval import is_schema_question


def test_schema_question_requires_an_introspection_request() -> None:
    assert is_schema_question("What safe retail tables and columns can you analyze?")
    assert not is_schema_question(
        "Show category return rates and explain that the available tables cannot prove why."
    )
