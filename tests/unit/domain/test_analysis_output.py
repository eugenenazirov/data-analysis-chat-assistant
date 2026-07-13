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


def test_narrative_output_allows_concise_derived_summary():
    rows = [
        {"product": "Boots", "revenue": 120},
        {"product": "Jeans", "revenue": 100},
        {"product": "Socks", "revenue": 80},
    ]
    fragments = ["Boots led revenue at 120; the other returned products trailed it."]

    assert narrative_output_violation(fragments, rows) is None
