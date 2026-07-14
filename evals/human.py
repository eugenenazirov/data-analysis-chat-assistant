from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.quality import QualityEvalCase, QualitySuiteResult
from retail_agent.models import ChartArtifact

DEFAULT_RUBRIC_PATH = Path("evals/rubrics/retail_analysis_v1.json")


class HumanEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReleaseThresholds(HumanEvaluationModel):
    minimum_reviewers: int = Field(ge=2)
    minimum_mean_usefulness: float = Field(ge=1, le=5)
    minimum_case_usefulness: float = Field(ge=1, le=5)
    major_disagreement_delta: int = Field(ge=1, le=4)
    minimum_pairwise_noninferiority_rate: float = Field(ge=0, le=1)
    minimum_live_repetitions: int = Field(ge=2)
    calibration_case_limit: int = Field(ge=1)
    minimum_case_dimension_score: float = Field(ge=1, le=5)
    blocking_dimensions: list[str]
    minimum_blocking_dimension_score: float = Field(ge=1, le=5)


class HumanRubric(HumanEvaluationModel):
    version: str
    scale: dict[str, str]
    dimensions: dict[str, str]
    release_thresholds: ReleaseThresholds

    @model_validator(mode="after")
    def validate_dimensions(self) -> HumanRubric:
        expected = set(HumanDimensionScores.model_fields)
        if set(self.dimensions) != expected:
            raise ValueError("rubric dimensions must match the structured score fields")
        if not set(self.release_thresholds.blocking_dimensions).issubset(expected):
            raise ValueError("blocking rubric dimensions must be score fields")
        return self


class BaselineCase(HumanEvaluationModel):
    case_id: str
    question: str
    history: list[str] = Field(default_factory=list)
    executed_sql: str
    verified_rows: list[dict[str, Any]]
    answer: str
    highlights: list[str] = Field(default_factory=list)
    chart: ChartArtifact | None = None


class AcceptedBaseline(HumanEvaluationModel):
    version: str
    accepted_at: datetime
    source_revision: str
    rubric_version: str
    cases: list[BaselineCase]

    @model_validator(mode="after")
    def validate_cases(self) -> AcceptedBaseline:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("accepted baseline case IDs must be unique")
        if not self.cases:
            raise ValueError("accepted baseline must contain at least one case")
        return self


class ReviewOutput(HumanEvaluationModel):
    executed_sql: str
    verified_rows: list[dict[str, Any]]
    answer: str
    highlights: list[str] = Field(default_factory=list)
    chart: ChartArtifact | None = None


class PairwiseComparison(HumanEvaluationModel):
    presentation_id: str
    output_a: ReviewOutput
    output_b: ReviewOutput


class HumanReviewFormCase(HumanEvaluationModel):
    case_id: str
    title: str
    risk: str
    question: str
    history: list[str]
    pointwise: ReviewOutput


class HumanReviewForm(HumanEvaluationModel):
    rubric_version: str
    instructions: list[str]
    dimensions: dict[str, str]
    cases: list[HumanReviewFormCase]


class PairwiseReviewFormCase(HumanEvaluationModel):
    case_id: str
    title: str
    risk: str
    question: str
    history: list[str]
    comparison: PairwiseComparison


class PairwiseReviewForm(HumanEvaluationModel):
    rubric_version: str
    instructions: list[str]
    cases: list[PairwiseReviewFormCase]


class BlindAssignment(HumanEvaluationModel):
    presentation_id: str
    case_id: str
    candidate_label: Literal["A", "B"]


class HumanReviewKey(HumanEvaluationModel):
    baseline_version: str
    assignments: list[BlindAssignment]

    @model_validator(mode="after")
    def validate_assignments(self) -> HumanReviewKey:
        presentation_ids = [item.presentation_id for item in self.assignments]
        case_ids = [item.case_id for item in self.assignments]
        if len(presentation_ids) != len(set(presentation_ids)):
            raise ValueError("blind presentation IDs must be unique")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("blind assignments must contain unique case IDs")
        return self


class HumanDimensionScores(HumanEvaluationModel):
    correctness: int = Field(ge=1, le=5)
    faithfulness: int = Field(ge=1, le=5)
    usefulness: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    limitations: int = Field(ge=1, le=5)
    privacy_and_policy: int = Field(ge=1, le=5)


class HumanReview(HumanEvaluationModel):
    case_id: str
    reviewer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
    rubric_version: str
    reviewed_at: datetime
    scores: HumanDimensionScores
    recommendation: Literal["approve", "revise", "reject"]
    presentation_id: str | None = None
    pairwise_preference: Literal["A", "B", "tie"] | None = None
    notes: str = ""
    resolution_notes: str | None = None

    @model_validator(mode="after")
    def validate_pairwise_fields(self) -> HumanReview:
        if (self.presentation_id is None) != (self.pairwise_preference is None):
            raise ValueError("presentation ID and pairwise preference must be supplied together")
        return self


class HumanReviewSet(HumanEvaluationModel):
    rubric_version: str
    reviews: list[HumanReview]

    @model_validator(mode="after")
    def validate_reviews(self) -> HumanReviewSet:
        identities = [(review.case_id, review.reviewer_id) for review in self.reviews]
        if len(identities) != len(set(identities)):
            raise ValueError("each reviewer may submit only one review per case")
        mismatched = [
            review.case_id
            for review in self.reviews
            if review.rubric_version != self.rubric_version
        ]
        if mismatched:
            raise ValueError("every review must use the review-set rubric version")
        return self

    def usefulness_scores(self) -> dict[str, float]:
        grouped: dict[str, list[int]] = defaultdict(list)
        for review in self.reviews:
            grouped[review.case_id].append(review.scores.usefulness)
        return {case_id: mean(scores) for case_id, scores in sorted(grouped.items())}


class HumanReviewSummary(HumanEvaluationModel):
    reviewer_count: int
    reviewed_case_count: int
    missing_cases: list[str]
    calibration_case_count: int
    required_calibration_cases: int
    mean_usefulness: float | None
    minimum_case_usefulness: float | None
    unresolved_disagreements: list[str]
    unresolved_rejections: list[str]
    minimum_dimension_scores: dict[str, float | None]
    pairwise_wins: int
    pairwise_ties: int
    pairwise_losses: int
    pairwise_noninferiority_rate: float | None


class ReleaseDecision(HumanEvaluationModel):
    approved: bool
    automated_passed: bool
    repeated_live_passed: bool
    human_review_passed: bool
    baseline_noninferiority_passed: bool
    baseline_version: str
    quality_dataset_sha256: str
    evaluated_at: datetime
    human_review: HumanReviewSummary
    blockers: list[str]


def load_accepted_baseline(path: Path) -> AcceptedBaseline:
    return AcceptedBaseline.model_validate_json(path.read_text(encoding="utf-8"))


@lru_cache
def load_human_rubric(path: Path = DEFAULT_RUBRIC_PATH) -> HumanRubric:
    return HumanRubric.model_validate_json(path.read_text(encoding="utf-8"))


def load_human_review_set(path: Path) -> HumanReviewSet:
    return HumanReviewSet.model_validate_json(path.read_text(encoding="utf-8"))


def load_human_review_key(path: Path) -> HumanReviewKey:
    return HumanReviewKey.model_validate_json(path.read_text(encoding="utf-8"))


def write_human_review_packet(
    cases: list[QualityEvalCase],
    result: QualitySuiteResult,
    baseline: AcceptedBaseline,
    *,
    form_path: Path,
    pairwise_path: Path,
    key_path: Path,
    seed: str,
    rubric: HumanRubric | None = None,
) -> tuple[HumanReviewForm, PairwiseReviewForm, HumanReviewKey]:
    resolved_rubric = rubric or load_human_rubric()
    if baseline.rubric_version != resolved_rubric.version:
        raise ValueError("accepted baseline and scoring form must use the same rubric version")
    mismatched_cases = [
        case.id for case in cases if case.human_rubric != resolved_rubric.version
    ]
    if mismatched_cases:
        raise ValueError("evaluation cases and scoring form must use the same rubric version")
    result_by_case = {
        item.name: item for item in result.results if item.attempt == 1 and item.diagnostics
    }
    baseline_by_case = {case.case_id: case for case in baseline.cases}
    assignments: list[BlindAssignment] = []
    form_cases: list[HumanReviewFormCase] = []
    pairwise_cases: list[PairwiseReviewFormCase] = []
    for case in cases:
        case_result = result_by_case.get(case.id)
        if case_result is None or case_result.diagnostics is None:
            raise ValueError(f"quality report is missing first-attempt diagnostics for {case.id}")
        diagnostics = case_result.diagnostics
        pointwise = ReviewOutput(
            executed_sql=diagnostics.candidate_sql,
            verified_rows=diagnostics.candidate_rows,
            answer=diagnostics.report_answer,
            highlights=diagnostics.report_highlights,
            chart=diagnostics.report_chart,
        )
        if previous := baseline_by_case.get(case.id):
            presentation_id = f"{case.id}-blind-v1"
            candidate_label: Literal["A", "B"] = (
                "A"
                if hashlib.sha256(f"{seed}:{case.id}".encode()).digest()[0] % 2 == 0
                else "B"
            )
            previous_output = ReviewOutput(
                executed_sql=previous.executed_sql,
                verified_rows=previous.verified_rows,
                answer=previous.answer,
                highlights=previous.highlights,
                chart=previous.chart,
            )
            comparison = PairwiseComparison(
                presentation_id=presentation_id,
                output_a=pointwise if candidate_label == "A" else previous_output,
                output_b=previous_output if candidate_label == "A" else pointwise,
            )
            assignments.append(
                BlindAssignment(
                    presentation_id=presentation_id,
                    case_id=case.id,
                    candidate_label=candidate_label,
                )
            )
            pairwise_cases.append(
                PairwiseReviewFormCase(
                    case_id=case.id,
                    title=case.title,
                    risk=case.risk,
                    question=case.question,
                    history=case.history,
                    comparison=comparison,
                )
            )
        form_cases.append(
            HumanReviewFormCase(
                case_id=case.id,
                title=case.title,
                risk=case.risk,
                question=case.question,
                history=case.history,
                pointwise=pointwise,
            )
        )

    form = HumanReviewForm(
        rubric_version=resolved_rubric.version,
        instructions=[
            "Receive this pointwise packet only after the blinded A/B review is submitted.",
            "Use the full 1-5 anchors; do not infer facts that are absent from verified rows.",
            "Use pseudonymous reviewer IDs and keep notes free of secrets and personal data.",
        ],
        dimensions=resolved_rubric.dimensions,
        cases=form_cases,
    )
    pairwise = PairwiseReviewForm(
        rubric_version=resolved_rubric.version,
        instructions=[
            "Complete and submit this A/B packet before receiving any pointwise packet.",
            "Select A, B, or tie without attempting to identify either output source.",
            "Do not compare this packet with candidate reports or other reviewer materials.",
        ],
        cases=pairwise_cases,
    )
    key = HumanReviewKey(baseline_version=baseline.version, assignments=assignments)
    _write_model(form, form_path)
    _write_model(pairwise, pairwise_path)
    _write_model(key, key_path)
    return form, pairwise, key


def evaluate_release_readiness(
    quality: QualitySuiteResult,
    reviews: HumanReviewSet,
    assignments: list[BlindAssignment],
    *,
    baseline_version: str,
    rubric: HumanRubric | None = None,
) -> ReleaseDecision:
    resolved_rubric = rubric or load_human_rubric()
    if reviews.rubric_version != resolved_rubric.version:
        raise ValueError("human reviews do not use the configured release rubric")
    thresholds = resolved_rubric.release_thresholds
    required_cases = sorted(
        {
            result.name
            for result in quality.results
            if "usefulness" in result.evaluators
        }
    )
    reviews_by_case: dict[str, list[HumanReview]] = defaultdict(list)
    for review in reviews.reviews:
        if review.case_id in required_cases:
            reviews_by_case[review.case_id].append(review)
    scores_by_case = {
        case_id: mean(review.scores.usefulness for review in case_reviews)
        for case_id, case_reviews in reviews_by_case.items()
    }
    missing_cases = sorted(set(required_cases) - set(reviews_by_case))
    reviewer_ids = {
        review.reviewer_id for case_reviews in reviews_by_case.values() for review in case_reviews
    }

    assignment_by_id = {assignment.presentation_id: assignment for assignment in assignments}
    expected_calibration_cases = {
        assignment.case_id for assignment in assignments if assignment.case_id in required_cases
    }
    calibrated_cases = {
        case_id
        for case_id in expected_calibration_cases
        if len({review.reviewer_id for review in reviews_by_case.get(case_id, [])}) >= 2
        and all(
            review.presentation_id in assignment_by_id
            for review in reviews_by_case.get(case_id, [])
        )
    }
    required_calibration_cases = min(
        thresholds.calibration_case_limit, len(expected_calibration_cases)
    )

    unresolved_disagreements: list[str] = []
    for case_id, case_reviews in reviews_by_case.items():
        dimension_ranges = [
            max(getattr(review.scores, dimension) for review in case_reviews)
            - min(getattr(review.scores, dimension) for review in case_reviews)
            for dimension in HumanDimensionScores.model_fields
        ]
        resolved = any((review.resolution_notes or "").strip() for review in case_reviews)
        if (
            dimension_ranges
            and max(dimension_ranges) >= thresholds.major_disagreement_delta
            and not resolved
        ):
            unresolved_disagreements.append(case_id)
    unresolved_rejections = sorted(
        {
            review.case_id
            for review in reviews.reviews
            if review.case_id in required_cases
            and review.recommendation == "reject"
            and not (review.resolution_notes or "").strip()
        }
    )

    minimum_dimension_scores: dict[str, float | None] = {}
    for dimension in HumanDimensionScores.model_fields:
        case_scores = [
            mean(getattr(review.scores, dimension) for review in case_reviews)
            for case_reviews in reviews_by_case.values()
        ]
        minimum_dimension_scores[dimension] = min(case_scores) if case_scores else None
    dimension_thresholds_passed = all(
        score is not None and score >= thresholds.minimum_case_dimension_score
        for score in minimum_dimension_scores.values()
    ) and all(
        minimum_dimension_scores[dimension] is not None
        and minimum_dimension_scores[dimension]
        >= thresholds.minimum_blocking_dimension_score
        for dimension in thresholds.blocking_dimensions
    )

    pairwise_wins = 0
    pairwise_ties = 0
    pairwise_losses = 0
    for review in reviews.reviews:
        if review.presentation_id is None or review.pairwise_preference is None:
            continue
        assignment = assignment_by_id.get(review.presentation_id)
        if assignment is None or assignment.case_id != review.case_id:
            continue
        if review.pairwise_preference == "tie":
            pairwise_ties += 1
        elif review.pairwise_preference == assignment.candidate_label:
            pairwise_wins += 1
        else:
            pairwise_losses += 1
    pairwise_count = pairwise_wins + pairwise_ties + pairwise_losses
    noninferiority_rate = (
        (pairwise_wins + pairwise_ties) / pairwise_count if pairwise_count else None
    )

    baseline_passed = (
        required_calibration_cases > 0
        and len(calibrated_cases) >= required_calibration_cases
        and noninferiority_rate is not None
        and noninferiority_rate >= thresholds.minimum_pairwise_noninferiority_rate
    )
    mean_usefulness = mean(scores_by_case.values()) if scores_by_case else None
    minimum_usefulness = min(scores_by_case.values()) if scores_by_case else None
    human_passed = (
        not missing_cases
        and len(reviewer_ids) >= thresholds.minimum_reviewers
        and not unresolved_disagreements
        and not unresolved_rejections
        and dimension_thresholds_passed
        and mean_usefulness is not None
        and mean_usefulness >= thresholds.minimum_mean_usefulness
        and minimum_usefulness is not None
        and minimum_usefulness >= thresholds.minimum_case_usefulness
        and baseline_passed
    )
    repeated_live_passed = (
        quality.mode == "live"
        and quality.repetitions >= thresholds.minimum_live_repetitions
        and not quality.flaky_cases
        and quality.operational is not None
        and quality.operational.first_attempt_success_rate == 1
        and quality.operational.attempt_success_rate == 1
    )
    blockers: list[str] = []
    if not quality.automated_passed:
        blockers.append("automated quality gates did not pass")
    if quality.mode != "live":
        blockers.append("credentialed live evaluation is required")
    elif quality.repetitions < thresholds.minimum_live_repetitions:
        blockers.append(
            f"at least {thresholds.minimum_live_repetitions} live repetitions per case are required"
        )
    elif not repeated_live_passed:
        blockers.append("repeated live reliability gate did not pass")
    if quality.flaky_cases:
        blockers.append("flaky live cases must be resolved")
    if len(reviewer_ids) < thresholds.minimum_reviewers:
        blockers.append(
            f"at least {thresholds.minimum_reviewers} independent reviewers are required"
        )
    if missing_cases:
        blockers.append("human usefulness review is incomplete")
    if unresolved_disagreements:
        blockers.append("major reviewer disagreements require resolution notes")
    if unresolved_rejections:
        blockers.append("reject recommendations require resolution notes and rescoring")
    if not dimension_thresholds_passed:
        blockers.append("human dimension thresholds were not met")
    if not baseline_passed:
        blockers.append("blinded baseline noninferiority was not demonstrated")
    if (
        mean_usefulness is not None
        and mean_usefulness < thresholds.minimum_mean_usefulness
    ):
        blockers.append(
            "mean human usefulness is below "
            f"{thresholds.minimum_mean_usefulness:g}/5"
        )
    if (
        minimum_usefulness is not None
        and minimum_usefulness < thresholds.minimum_case_usefulness
    ):
        blockers.append(
            "a case has human usefulness below "
            f"{thresholds.minimum_case_usefulness:g}/5"
        )

    summary = HumanReviewSummary(
        reviewer_count=len(reviewer_ids),
        reviewed_case_count=len(reviews_by_case),
        missing_cases=missing_cases,
        calibration_case_count=len(calibrated_cases),
        required_calibration_cases=required_calibration_cases,
        mean_usefulness=mean_usefulness,
        minimum_case_usefulness=minimum_usefulness,
        unresolved_disagreements=sorted(unresolved_disagreements),
        unresolved_rejections=unresolved_rejections,
        minimum_dimension_scores=minimum_dimension_scores,
        pairwise_wins=pairwise_wins,
        pairwise_ties=pairwise_ties,
        pairwise_losses=pairwise_losses,
        pairwise_noninferiority_rate=noninferiority_rate,
    )
    approved = quality.automated_passed and repeated_live_passed and human_passed
    return ReleaseDecision(
        approved=approved,
        automated_passed=quality.automated_passed,
        repeated_live_passed=repeated_live_passed,
        human_review_passed=human_passed,
        baseline_noninferiority_passed=baseline_passed,
        baseline_version=baseline_version,
        quality_dataset_sha256=quality.versions.dataset_sha256,
        evaluated_at=datetime.now().astimezone(),
        human_review=summary,
        blockers=blockers,
    )


def write_release_decision(decision: ReleaseDecision, path: Path) -> None:
    _write_model(decision, path)


def _write_model(model: BaseModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")
