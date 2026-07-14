import asyncio
from collections import Counter
from pathlib import Path

from evals.dataset import inspect_dataset_governance
from evals.datasets.build_replay_fixtures import (
    HOLDOUT_SCENARIOS,
    MULTI_TURN_SCENARIOS,
    _render,
)
from evals.quality import (
    _conversation_assessment,
    evaluate_quality_case,
    load_quality_cases,
    run_quality_live_evals,
    run_quality_replay_evals,
)
from retail_agent.agent import TurnResult
from retail_agent.models import AnalysisReport
from retail_agent.observability import EventLogger

HOLDOUT_PATH = Path("evals/datasets/release_holdout.jsonl")
MULTI_TURN_PATH = Path("evals/datasets/multi_turn.jsonl")
GOLDEN_PATH = Path("data/golden_trios.jsonl")


def test_release_holdout_meets_portfolio_coverage_contract(test_config):
    cases = load_quality_cases(HOLDOUT_PATH)
    categories = Counter(case.category for case in cases)
    elevated_risk = sum(case.risk in {"high", "critical"} for case in cases)
    result = run_quality_replay_evals(test_config, HOLDOUT_PATH)

    assert len(cases) == 30
    assert max(categories.values()) / len(cases) <= 0.2
    assert elevated_risk / len(cases) >= 0.25
    assert all(case.modes == {"replay", "live"} for case in cases)
    assert result.automated_passed is True
    assert result.passed is False
    assert result.needs_human_review is True
    assert result.critical_failures == []


def test_multi_turn_suite_scores_each_real_conversation(test_config):
    cases = load_quality_cases(MULTI_TURN_PATH)
    result = run_quality_replay_evals(test_config, MULTI_TURN_PATH)

    assert len(cases) == 10
    assert all(1 <= len(case.history) <= 4 for case in cases)
    assert all(case.conversation_contract is not None for case in cases)
    assert result.metrics["multi_turn"].applicable_cases == 10
    assert result.metrics["multi_turn"].passed_cases == 10
    assert result.automated_passed is True


def test_generalization_suites_have_no_golden_knowledge_overlap():
    for path in (HOLDOUT_PATH, MULTI_TURN_PATH):
        governance = inspect_dataset_governance(load_quality_cases(path), GOLDEN_PATH)

        assert governance.golden_question_overlap_ids == []
        assert governance.golden_sql_overlap_ids == []
        assert governance.intentional_overlap_count == 0


def test_generated_replay_fixtures_are_current():
    assert HOLDOUT_PATH.read_text(encoding="utf-8") == _render(
        HOLDOUT_SCENARIOS, "release_holdout"
    )
    assert MULTI_TURN_PATH.read_text(encoding="utf-8") == _render(
        MULTI_TURN_SCENARIOS, "multi_turn"
    )


def test_expected_clarification_passes_without_fabricated_sql(test_config):
    case = next(
        case for case in load_quality_cases(HOLDOUT_PATH) if case.expected_behavior == "clarify"
    )

    result = evaluate_quality_case(test_config, case, case.replay)

    assert result.automated_passed is True
    assert result.scores.intent is None
    assert result.scores.calculation is None
    assert result.diagnostics.expected_behavior_met is True
    assert result.diagnostics.verified_sql_attached is False


def test_failed_history_sql_cannot_become_trusted(test_config):
    case = next(
        case
        for case in load_quality_cases(MULTI_TURN_PATH)
        if case.id == "failed_turn_recovery_lineage"
    )
    bad_turns = [
        case.replay.history_turns[0].model_copy(update={"trusted": True}),
        *case.replay.history_turns[1:],
    ]
    replay = case.replay.model_copy(update={"history_turns": bad_turns})

    diagnostics = _conversation_assessment(case, replay)

    assert any(
        item.name == "tool_result_lineage" and not item.passed for item in diagnostics
    )


class CanonicalQueryMustNotRun:
    def execute(self, sql: str, trace_id: str):
        raise AssertionError("clarification must not execute a canonical query")


def test_live_clarification_does_not_require_query_result(
    test_config, tmp_path, monkeypatch
):
    case = next(
        case for case in load_quality_cases(HOLDOUT_PATH) if case.expected_behavior == "clarify"
    )
    path = tmp_path / "clarification.jsonl"
    path.write_text(case.model_dump_json() + "\n", encoding="utf-8")

    async def fake_run_question(question, *, conversation, **kwargs):
        return TurnResult(
            response=AnalysisReport(question=question, answer=case.replay.report.answer),
            conversation=conversation,
        )

    monkeypatch.setattr("evals.quality.run_question", fake_run_question)
    result = asyncio.run(
        run_quality_live_evals(
            test_config,
            path,
            bigquery=CanonicalQueryMustNotRun(),
            golden_store=object(),
            logger=EventLogger(tmp_path / "runs.jsonl"),
            analysis_agent=object(),
            human_scores={case.id: 5},
            max_safe_attempts=1,
        )
    )

    assert result.passed is True
    assert result.results[0].diagnostics.expected_behavior_met is True
