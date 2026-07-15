import pytest

from retail_agent.domain.policies.retrieval import (
    is_schema_question,
    requires_golden_precedent,
)


@pytest.mark.parametrize(
    ("question", "has_history"),
    [
        ("Which product categories drove the most revenue last month?", False),
        ("Who are our top customers by spend?", False),
        ("Which products have high return risk?", False),
        ("Analyze customer behavior by region", False),
        ("Show year-to-date revenue", False),
        ("What happened yesterday?", False),
        ("Revenue over the past 90 days", False),
        ("Revenue for the last 90 days", False),
        ("Revenue in the past week", False),
        ("Revenue for this quarter", False),
        ("Which one lost more revenue within that same cohort?", True),
        ("What about California?", True),
        ("Show a table of top customers by spend", False),
        ("Which tables had the most returns?", False),
        ("Compare order tables by monthly row counts", False),
    ],
)
def test_precedent_is_required_for_high_risk_analysis(question, has_history):
    assert requires_golden_precedent(question, has_history=has_history)


@pytest.mark.parametrize(
    ("question", "has_history"),
    [
        ("Explain the customer table schema", False),
        ("Which columns describe orders?", False),
        ("What tables are available?", False),
        ("How many orders are there?", False),
        ("What does that mean?", False),
    ],
)
def test_precedent_remains_optional_for_schema_and_simple_questions(
    question, has_history
):
    assert not requires_golden_precedent(question, has_history=has_history)


def test_schema_question_requires_an_introspection_request() -> None:
    assert is_schema_question("What safe retail tables and columns can you analyze?")
    assert not is_schema_question(
        "Show category return rates and explain that the available tables cannot prove why."
    )
