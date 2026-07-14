import pytest

from retail_agent.infrastructure.prompts.builder import (
    PromptResourceError,
    build_analysis_prompt,
    load_prompt_template,
)


def test_analysis_prompt_combines_versioned_role_and_safety_rules():
    prompt = build_analysis_prompt()

    assert prompt.version == "analysis-v4"
    assert "retail data analysis assistant" in prompt.instructions
    assert "Safety rules:" in prompt.instructions
    assert "personally identifiable information" in prompt.instructions
    assert "must call retrieve_golden_examples" in prompt.instructions
    assert "audit every numeric value" in prompt.instructions
    assert "instead of enumerating query rows" in prompt.instructions


def test_missing_prompt_resource_fails_clearly():
    with pytest.raises(PromptResourceError, match="is unavailable"):
        load_prompt_template("missing-prompt.md")
