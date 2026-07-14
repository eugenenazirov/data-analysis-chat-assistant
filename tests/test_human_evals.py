from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from evals.human import (
    AcceptedBaseline,
    BaselineCase,
    BlindAssignment,
    HumanDimensionScores,
    HumanReview,
    HumanReviewSet,
    evaluate_release_readiness,
    load_accepted_baseline,
    load_human_review_set,
    load_human_rubric,
    write_human_review_packet,
    write_release_decision,
)
from evals.quality import (
    load_human_scores,
    load_quality_cases,
    run_quality_replay_evals,
    summarize_quality_results,
)

CASES_PATH = Path("evals/datasets/smoke.jsonl")
BASELINE_PATH = Path("evals/baselines/accepted-smoke-v1.json")


def _baseline() -> AcceptedBaseline:
    cases = load_quality_cases(CASES_PATH)
    return AcceptedBaseline(
        version="accepted-smoke-v1",
        accepted_at=datetime(2026, 7, 13, tzinfo=UTC),
        source_revision="f6e0eda",
        rubric_version="retail_analysis_v1",
        cases=[
            BaselineCase(
                case_id=case.id,
                question=case.question,
                history=case.history,
                executed_sql=case.replay.candidate_sql,
                verified_rows=case.replay.candidate_rows,
                answer=f"Prior accepted answer for {case.id}.",
                highlights=[],
                chart=None,
            )
            for case in cases
        ],
    )


def _reviews(*, score: int = 5, preference: str = "tie") -> HumanReviewSet:
    cases = load_quality_cases(CASES_PATH)
    reviews = []
    for case in cases:
        for reviewer_id in ("analyst-01", "analyst-02"):
            reviews.append(
                HumanReview(
                    case_id=case.id,
                    reviewer_id=reviewer_id,
                    rubric_version="retail_analysis_v1",
                    reviewed_at=datetime(2026, 7, 14, tzinfo=UTC),
                    scores=HumanDimensionScores(
                        correctness=score,
                        faithfulness=score,
                        usefulness=score,
                        clarity=score,
                        limitations=score,
                        privacy_and_policy=score,
                    ),
                    recommendation="approve",
                    presentation_id=f"{case.id}-blind-v1",
                    pairwise_preference=preference,
                    notes="No material issue found.",
                )
            )
    return HumanReviewSet(rubric_version="retail_analysis_v1", reviews=reviews)


def _blind_assignments(candidate_label: str = "A") -> list[BlindAssignment]:
    return [
        BlindAssignment(
            presentation_id=f"{case.id}-blind-v1",
            case_id=case.id,
            candidate_label=candidate_label,
        )
        for case in load_quality_cases(CASES_PATH)
    ]


def test_committed_rubric_and_baseline_are_compatible_with_calibration_suite():
    rubric = load_human_rubric()
    baseline = load_accepted_baseline(BASELINE_PATH)

    assert rubric.version == baseline.rubric_version == "retail_analysis_v1"
    assert {case.case_id for case in baseline.cases} == {
        case.id for case in load_quality_cases(CASES_PATH)
    }
    assert rubric.release_thresholds.minimum_reviewers == 2
    assert rubric.release_thresholds.minimum_live_repetitions == 5


def test_review_packet_contains_required_context_and_keeps_pairwise_sources_blind(
    test_config, tmp_path
):
    result = run_quality_replay_evals(test_config, CASES_PATH)
    form_path = tmp_path / "review-form.json"
    pairwise_path = tmp_path / "pairwise-form.json"
    key_path = tmp_path / "review-key.json"

    form, pairwise, key = write_human_review_packet(
        load_quality_cases(CASES_PATH),
        result,
        _baseline(),
        form_path=form_path,
        pairwise_path=pairwise_path,
        key_path=key_path,
        seed="release-2026-07-14",
    )

    first = form.cases[0]
    assert first.question
    assert first.pointwise.executed_sql
    assert first.pointwise.verified_rows
    assert first.pointwise.answer
    assert first.pointwise.highlights == []
    assert first.pointwise.chart is None
    comparison = pairwise.cases[0]
    pairwise_payload = comparison.model_dump_json().casefold()
    assert "candidate" not in pairwise_payload
    assert "baseline" not in pairwise_payload
    assert key.assignments[0].candidate_label in {"A", "B"}
    assert json.loads(form_path.read_text(encoding="utf-8"))["rubric_version"] == (
        "retail_analysis_v1"
    )
    pairwise_file = json.loads(pairwise_path.read_text(encoding="utf-8"))
    assert all("pointwise" not in item for item in pairwise_file["cases"])
    assert json.loads(key_path.read_text(encoding="utf-8"))["baseline_version"] == (
        "accepted-smoke-v1"
    )


def test_structured_human_scores_are_aggregated_per_case(tmp_path):
    reviews = _reviews(score=4)
    path = tmp_path / "reviews.json"
    path.write_text(reviews.model_dump_json(indent=2), encoding="utf-8")

    assert load_human_review_set(path) == reviews
    assert load_human_scores(path) == {
        case.id: 4.0 for case in load_quality_cases(CASES_PATH)
    }


def test_release_requires_live_repetitions_two_reviewers_and_baseline_noninferiority(
    test_config,
):
    replay = run_quality_replay_evals(test_config, CASES_PATH)
    repeated = [
        result.model_copy(update={"attempt": attempt})
        for result in replay.results
        for attempt in range(1, 6)
    ]
    live = summarize_quality_results(
        "live",
        repeated,
        versions=replay.versions,
        governance=replay.governance,
    )
    reviews = _reviews()

    replay_decision = evaluate_release_readiness(
        replay, reviews, _blind_assignments(), baseline_version="accepted-smoke-v1"
    )
    live_decision = evaluate_release_readiness(
        live, reviews, _blind_assignments(), baseline_version="accepted-smoke-v1"
    )
    one_reviewer = reviews.model_copy(
        update={
            "reviews": [review for review in reviews.reviews if review.reviewer_id == "analyst-01"]
        }
    )
    one_reviewer_decision = evaluate_release_readiness(
        live,
        one_reviewer,
        _blind_assignments(),
        baseline_version="accepted-smoke-v1",
    )

    assert replay_decision.approved is False
    assert "credentialed live evaluation is required" in replay_decision.blockers
    assert live_decision.approved is True
    assert live_decision.human_review.reviewer_count == 2
    assert live_decision.human_review.calibration_case_count == 4
    assert one_reviewer_decision.approved is False
    assert "at least 2 independent reviewers are required" in one_reviewer_decision.blockers


def test_pairwise_outcome_is_resolved_from_blind_key_not_display_position(test_config):
    replay = run_quality_replay_evals(test_config, CASES_PATH)
    choose_a = _reviews(preference="A")

    candidate_a = evaluate_release_readiness(
        replay, choose_a, _blind_assignments("A"), baseline_version="accepted-smoke-v1"
    )
    candidate_b = evaluate_release_readiness(
        replay, choose_a, _blind_assignments("B"), baseline_version="accepted-smoke-v1"
    )

    assert candidate_a.human_review.pairwise_wins == 8
    assert candidate_a.human_review.pairwise_losses == 0
    assert candidate_b.human_review.pairwise_wins == 0
    assert candidate_b.human_review.pairwise_losses == 8


def test_major_reviewer_disagreement_blocks_until_resolution(test_config):
    replay = run_quality_replay_evals(test_config, CASES_PATH)
    reviews = _reviews()
    first_case = load_quality_cases(CASES_PATH)[0].id
    changed = []
    for review in reviews.reviews:
        if review.case_id == first_case and review.reviewer_id == "analyst-02":
            changed.append(
                review.model_copy(
                    update={
                        "scores": review.scores.model_copy(update={"usefulness": 2}),
                        "recommendation": "revise",
                    }
                )
            )
        else:
            changed.append(review)

    unresolved = reviews.model_copy(update={"reviews": changed})
    decision = evaluate_release_readiness(
        replay,
        unresolved,
        _blind_assignments(),
        baseline_version="accepted-smoke-v1",
    )

    assert decision.approved is False
    assert first_case in decision.human_review.unresolved_disagreements


def test_low_critical_dimension_or_reject_recommendation_blocks_release(test_config):
    replay = run_quality_replay_evals(test_config, CASES_PATH)
    repeated = [
        result.model_copy(update={"attempt": attempt})
        for result in replay.results
        for attempt in range(1, 6)
    ]
    live = summarize_quality_results(
        "live", repeated, versions=replay.versions, governance=replay.governance
    )
    low_privacy_reviews = _reviews()
    low_privacy_reviews = low_privacy_reviews.model_copy(
        update={
            "reviews": [
                review.model_copy(
                    update={
                        "scores": review.scores.model_copy(update={"privacy_and_policy": 1})
                    }
                )
                for review in low_privacy_reviews.reviews
            ]
        }
    )
    reject_reviews = _reviews()
    reject_reviews = reject_reviews.model_copy(
        update={
            "reviews": [
                review.model_copy(update={"recommendation": "reject"})
                if index == 0
                else review
                for index, review in enumerate(reject_reviews.reviews)
            ]
        }
    )

    low_privacy = evaluate_release_readiness(
        live,
        low_privacy_reviews,
        _blind_assignments(),
        baseline_version="accepted-smoke-v1",
    )
    rejected = evaluate_release_readiness(
        live,
        reject_reviews,
        _blind_assignments(),
        baseline_version="accepted-smoke-v1",
    )

    assert low_privacy.approved is False
    assert "human dimension thresholds were not met" in low_privacy.blockers
    assert rejected.approved is False
    assert rejected.human_review.unresolved_rejections


def test_release_decision_round_trips(test_config, tmp_path):
    replay = run_quality_replay_evals(test_config, CASES_PATH)
    decision = evaluate_release_readiness(
        replay, _reviews(), _blind_assignments(), baseline_version="accepted-smoke-v1"
    )
    path = tmp_path / "release-decision.json"

    write_release_decision(decision, path)

    assert json.loads(path.read_text(encoding="utf-8"))["approved"] is False
