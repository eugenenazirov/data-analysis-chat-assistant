import pytest

from retail_agent.infrastructure.charts.templates import TESTED_CHART_TEMPLATES
from retail_agent.infrastructure.prompts.builder import (
    PromptResourceError,
    build_analysis_prompt,
    load_prompt_template,
)


def test_analysis_prompt_combines_versioned_role_and_safety_rules():
    prompt = build_analysis_prompt()

    assert prompt.version == "analysis-v12"
    assert "Do not calculate or state averages, ratios, percentages" in prompt.instructions
    assert "must not introduce a new number" in prompt.instructions
    assert "retail data analysis assistant" in prompt.instructions
    assert "Safety rules:" in prompt.instructions
    assert "personally identifiable information" in prompt.instructions
    assert "Choose retrieve_golden_examples" in prompt.instructions
    assert "must call retrieve_golden_examples" not in prompt.instructions
    assert "audit every numeric value" in prompt.instructions
    assert "instead of enumerating query rows" in prompt.instructions
    assert "Default to PNG" in prompt.instructions
    assert "156 cells" in prompt.instructions
    assert "Match the requested grain and scope exactly" in prompt.instructions
    assert "never use `CURRENT_DATE()`" in prompt.instructions
    assert "putting product name inside the ranking window destroys" in prompt.instructions
    assert all(template.code.strip() in prompt.instructions for template in TESTED_CHART_TEMPLATES)


def test_missing_prompt_resource_fails_clearly():
    with pytest.raises(PromptResourceError, match="is unavailable"):
        load_prompt_template("missing-prompt.md")
