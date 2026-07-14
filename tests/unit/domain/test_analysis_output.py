from decimal import Decimal

from retail_agent.domain.policies.analysis_output import narrative_output_violation


def test_narrative_output_rejects_markdown_table():
    fragments = ["| Product | Revenue |\n| --- | ---: |\n| Boots | 120 |\n| Jeans | 100 |"]

    assert narrative_output_violation(fragments, []) == "markdown_table"


def test_narrative_output_rejects_row_by_row_dump():
    rows = [
        {"product": "Boots", "revenue": 1_549.0},
        {"product": "Jeans", "revenue": 100},
        {"product": "Socks", "revenue": 80},
    ]
    fragments = ["Boots: $1,549\nJeans: $100\nSocks: $80"]

    assert narrative_output_violation(fragments, rows) == "row_dump"


def test_narrative_output_rejects_large_precise_row_dump():
    rows = [
        {"category": "Alpha", "revenue": Decimal("1330431.52")},
        {"category": "Beta", "revenue": 975_000.25},
        {"category": "Gamma", "revenue": 825_100.75},
    ]
    fragments = [
        "Alpha — 1330431.52",
        "Beta — 975000.25",
        "Gamma — 825100.75",
    ]

    assert narrative_output_violation(fragments, rows) == "row_dump"


def test_narrative_output_allows_concise_derived_summary():
    rows = [
        {"product": "Boots", "revenue": 120},
        {"product": "Jeans", "revenue": 100},
        {"product": "Socks", "revenue": 80},
    ]
    fragments = ["Boots led revenue at 120; the other returned products trailed it."]

    assert narrative_output_violation(fragments, rows) is None
