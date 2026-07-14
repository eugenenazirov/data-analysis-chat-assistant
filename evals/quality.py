from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import mean, pvariance
from typing import Any, Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp
from sqlglot.errors import SqlglotError

from retail_agent.agent import ConversationState, TurnResult, run_question
from retail_agent.application.ports import AnalyticsGateway, GoldenExampleRepository
from retail_agent.config import AgentConfig
from retail_agent.domain.policies.report_evidence import (
    assess_report_evidence,
    report_uses_verified_sql,
)
from retail_agent.infrastructure.prompts.builder import build_analysis_prompt
from retail_agent.models import AgentFailure, AnalysisReport
from retail_agent.observability import EventLogger, new_trace_id
from retail_agent.ports import AnalysisAgentPort
from retail_agent.sql_guard import normalized_table_aliases, validate_and_prepare_sql

type QualityMode = Literal["replay", "live"]
type EvaluationSuite = Literal[
    "smoke", "development", "regression", "release_holdout", "adversarial", "multi_turn"
]
type EvaluationRisk = Literal["low", "medium", "high", "critical"]
type ExpectedBehavior = Literal["answer", "clarify", "refuse", "degrade"]
type ResultUnit = Literal["currency", "percentage", "count", "identifier", "text", "date"]
type EvaluatorName = Literal[
    "intent",
    "calculation",
    "retrieval",
    "faithfulness",
    "multi_turn",
    "usefulness",
]
type ScoreName = Literal[
    "intent",
    "calculation",
    "retrieval",
    "retrieval_mrr",
    "faithfulness",
    "multi_turn",
    "usefulness",
]

EVALUATOR_VERSION = "quality-v2"
_AUTOMATED_EVALUATORS: tuple[EvaluatorName, ...] = (
    "intent",
    "calculation",
    "retrieval",
    "faithfulness",
    "multi_turn",
)


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JoinKey(EvaluationModel):
    left: str
    right: str

    def normalized(self) -> tuple[str, str]:
        return tuple(sorted((self.left.lower(), self.right.lower())))


class QualityExpectations(EvaluationModel):
    required_tables: list[str] = Field(default_factory=list)
    allowed_joins: list[JoinKey] = Field(default_factory=list)
    required_sql_fragments: list[str] = Field(default_factory=list)
    forbidden_sql_fragments: list[str] = Field(default_factory=list)
    expected_retrieval_ids: list[str] = Field(default_factory=list)
    numeric_tolerance: float = 0.001


class ResultContract(EvaluationModel):
    key_columns: list[str] = Field(default_factory=list)
    measure_columns: list[str] = Field(default_factory=list)
    column_mapping: dict[str, str]
    ordered: bool = False
    numeric_tolerance: float = Field(default=0.001, ge=0, le=0.1)
    units: dict[str, ResultUnit] = Field(default_factory=dict)


class AnswerContract(EvaluationModel):
    required_facts: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    pii_forbidden: bool = True


class FixtureProvenance(EvaluationModel):
    canonical_sql_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reference_date: date
    bigquery_location: str
    source_datasets: list[str] = Field(min_length=1)
    source_tables: list[str] = Field(min_length=1)
    result_schema: dict[str, str]
    row_count: int = Field(ge=0)
    captured_at: datetime
    evaluator_version: str
    prompt_version: str
    persona_version: str
    model: str
    embedding_model: str
    golden_index_version: str
    from_cache: bool
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class QualityReplay(EvaluationModel):
    candidate_sql: str
    candidate_rows: list[dict[str, Any]]
    canonical_rows: list[dict[str, Any]]
    retrieved_ids: list[str]
    report: AnalysisReport
    history_used: bool = False
    usefulness_score: float | None = Field(default=None, ge=0, le=5)
    reference_date: date | None = None
    provenance: FixtureProvenance


class QualityEvalCase(EvaluationModel):
    id: str
    title: str
    suite: EvaluationSuite
    category: str
    risk: EvaluationRisk
    question: str
    user_id: str = "manager_a"
    history: list[str] = Field(default_factory=list)
    reference_date: date
    expected_behavior: ExpectedBehavior
    modes: set[QualityMode]
    evaluators: set[EvaluatorName]
    canonical_sql: str
    expectations: QualityExpectations
    result_contract: ResultContract
    answer_contract: AnswerContract
    replay: QualityReplay
    critical: bool = False

    @model_validator(mode="after")
    def validate_contract(self) -> QualityEvalCase:
        if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", self.id):
            raise ValueError("case id must be stable snake_case")
        if not self.title.strip() or not self.category.strip():
            raise ValueError("title and category must not be blank")
        if "replay" not in self.modes:
            raise ValueError("every committed case must support replay mode")
        if "multi_turn" in self.evaluators and not self.history:
            raise ValueError("multi_turn evaluator requires conversation history")
        if self.history and "multi_turn" not in self.evaluators:
            raise ValueError("conversation history requires the multi_turn evaluator")
        if self.critical and self.risk not in {"high", "critical"}:
            raise ValueError("critical cases must declare high or critical risk")
        if self.replay.reference_date not in {None, self.reference_date}:
            raise ValueError("replay reference date must match the case reference date")
        provenance = self.replay.provenance
        if provenance.reference_date != self.reference_date:
            raise ValueError("fixture provenance reference date must match the case")
        if provenance.canonical_sql_sha256 != _sha256_text(self.canonical_sql):
            raise ValueError("fixture canonical SQL hash does not match canonical_sql")
        if provenance.row_count != len(self.replay.canonical_rows):
            raise ValueError("fixture row count does not match canonical rows")
        if provenance.result_schema != _result_schema(self.replay.canonical_rows):
            raise ValueError("fixture result schema does not match canonical rows")
        if provenance.content_sha256 != _fixture_content_sha256(
            self.canonical_sql, self.replay
        ):
            raise ValueError("fixture content hash does not match replay content")
        canonical_columns = set(provenance.result_schema)
        declared_columns = {
            *self.result_contract.key_columns,
            *self.result_contract.measure_columns,
        }
        if not declared_columns.issubset(canonical_columns):
            raise ValueError("result contract references unknown canonical columns")
        if not canonical_columns.issubset(self.result_contract.column_mapping):
            raise ValueError("result contract must map every canonical result column")
        if not set(self.result_contract.units).issubset(canonical_columns):
            raise ValueError("result units reference unknown canonical columns")
        if self.result_contract.numeric_tolerance != self.expectations.numeric_tolerance:
            raise ValueError("result and SQL expectation tolerances must match")
        return self


class QualityScores(EvaluationModel):
    intent: float | None
    calculation: float | None
    retrieval: float | None
    retrieval_mrr: float | None
    faithfulness: float | None
    multi_turn: float | None
    usefulness: float | None


class ConstraintDiagnostic(EvaluationModel):
    name: str
    passed: bool
    detail: str


class IntentAssessment(EvaluationModel):
    score: float
    constraints: list[ConstraintDiagnostic]


class ResultAssessment(EvaluationModel):
    score: float
    violations: list[str] = Field(default_factory=list)


class QualityDiagnostics(EvaluationModel):
    unsupported_numeric_claims: list[float] = Field(default_factory=list)
    candidate_sql: str
    candidate_rows: list[dict[str, Any]]
    canonical_rows: list[dict[str, Any]]
    report_answer: str
    report_highlights: list[str] = Field(default_factory=list)
    retrieved_ids: list[str] = Field(default_factory=list)
    history_used: bool = False
    report_degraded: bool = False
    report_refused: bool = False
    reference_date: date | None = None
    constraint_results: list[ConstraintDiagnostic] = Field(default_factory=list)
    result_contract_violations: list[str] = Field(default_factory=list)
    unsupported_qualitative_claims: list[str] = Field(default_factory=list)
    verified_sql_attached: bool = False


class QualityEvalResult(EvaluationModel):
    name: str
    suite: EvaluationSuite
    category: str
    risk: EvaluationRisk
    evaluators: set[EvaluatorName]
    passed: bool
    automated_passed: bool
    scores: QualityScores
    detail: str
    needs_human_review: bool = False
    critical: bool = False
    diagnostics: QualityDiagnostics | None = None


class MetricSummary(EvaluationModel):
    applicable_cases: int
    passed_cases: int
    minimum: float | None
    mean: float | None
    variance: float | None


class EvaluationVersions(EvaluationModel):
    application: str = "0.1.0"
    evaluator: str = EVALUATOR_VERSION
    dataset_sha256: str = "unknown"
    prompt: str = "unknown"
    persona: str = "unknown"
    model: str = "unknown"
    embedding_model: str = "unknown"
    golden_index: str = "unknown"
    reference_dates: list[date] = Field(default_factory=list)


class DatasetGovernance(EvaluationModel):
    golden_question_overlap_ids: list[str] = Field(default_factory=list)
    golden_sql_overlap_ids: list[str] = Field(default_factory=list)
    intentional_overlap_count: int = 0


class QualitySuiteResult(EvaluationModel):
    mode: QualityMode
    passed: bool
    automated_passed: bool
    results: list[QualityEvalResult]
    aggregate: QualityScores
    case_count: int
    suite_counts: dict[str, int] = Field(default_factory=dict)
    category_counts: dict[str, int] = Field(default_factory=dict)
    risk_counts: dict[str, int] = Field(default_factory=dict)
    metrics: dict[ScoreName, MetricSummary] = Field(default_factory=dict)
    critical_failures: list[str] = Field(default_factory=list)
    versions: EvaluationVersions = Field(default_factory=EvaluationVersions)
    governance: DatasetGovernance = Field(default_factory=DatasetGovernance)
    needs_human_review: bool = False


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fixture_content_sha256(canonical_sql: str, replay: QualityReplay) -> str:
    payload = {
        "canonical_sql": canonical_sql,
        "replay": replay.model_dump(mode="json", exclude={"provenance"}),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _sha256_text(serialized)


def _result_schema(rows: list[dict[str, Any]]) -> dict[str, str]:
    field_types: dict[str, set[str]] = {}
    for row in rows:
        for field, value in row.items():
            field_types.setdefault(field, set()).add(_value_type_name(value))
    return {
        field: " | ".join(sorted(types))
        for field, types in sorted(field_types.items())
    }


def _value_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def load_quality_cases(path: Path) -> list[QualityEvalCase]:
    cases: list[QualityEvalCase] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    cases.append(QualityEvalCase.model_validate_json(line))
                except (ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"Invalid evaluation case at {path}:{line_number}: {exc}"
                    ) from exc
    if not cases:
        raise ValueError(f"Evaluation dataset is empty: {path}")
    duplicate_ids = sorted(
        case_id for case_id, count in Counter(c.id for c in cases).items() if count > 1
    )
    if duplicate_ids:
        raise ValueError(f"Duplicate evaluation case IDs: {', '.join(duplicate_ids)}")
    return cases


def run_quality_replay_evals(config: AgentConfig, path: Path) -> QualitySuiteResult:
    from evals.dataset import inspect_dataset_governance, validate_partition_path

    cases = load_quality_cases(path)
    validate_partition_path(path, cases)
    governance = inspect_dataset_governance(cases, config.golden_trios_path)
    results = [evaluate_quality_case(config, case, case.replay) for case in cases]
    return summarize_quality_results(
        "replay",
        results,
        versions=_evaluation_versions(config, path, cases),
        governance=governance,
    )


async def run_quality_live_evals(
    config: AgentConfig,
    path: Path,
    *,
    bigquery: AnalyticsGateway,
    golden_store: GoldenExampleRepository,
    logger: EventLogger,
    analysis_agent: AnalysisAgentPort,
    human_scores: dict[str, float] | None = None,
    max_safe_attempts: int = 3,
    retry_delay_seconds: float = 5.0,
) -> QualitySuiteResult:
    from evals.dataset import inspect_dataset_governance, validate_partition_path

    results: list[QualityEvalResult] = []
    cases = load_quality_cases(path)
    validate_partition_path(path, cases)
    governance = inspect_dataset_governance(cases, config.golden_trios_path)
    for case in cases:
        conversation = ConversationState()
        history_succeeded = True
        for question in case.history:
            history_turn = await _run_live_turn(
                question,
                config=config,
                bigquery=bigquery,
                golden_store=golden_store,
                logger=logger,
                user_id=case.user_id,
                conversation=conversation,
                analysis_agent=analysis_agent,
                max_safe_attempts=max_safe_attempts,
                retry_delay_seconds=retry_delay_seconds,
            )
            conversation = history_turn.conversation
            if isinstance(history_turn.response, AgentFailure):
                history_succeeded = False
                break

        if not history_succeeded:
            results.append(
                _failed_live_result(case, "history turn failed")
            )
            continue

        turn = await _run_live_turn(
            case.question,
            config=config,
            bigquery=bigquery,
            golden_store=golden_store,
            logger=logger,
            user_id=case.user_id,
            conversation=conversation,
            analysis_agent=analysis_agent,
            max_safe_attempts=max_safe_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )
        if isinstance(turn.response, AgentFailure) or turn.query_result is None:
            results.append(
                _failed_live_result(case, "candidate agent run failed")
            )
            continue

        try:
            canonical = bigquery.execute(case.canonical_sql, new_trace_id())
        except Exception as exc:
            results.append(
                _failed_live_result(case, f"canonical query failed: {exc.__class__.__name__}")
            )
            continue

        replay = case.replay.model_copy(
            update={
                "candidate_sql": turn.query_result.sql,
                "candidate_rows": turn.query_result.rows,
                "canonical_rows": canonical.rows,
                "retrieved_ids": list(turn.retrieved_trio_ids),
                "report": turn.response,
                "history_used": bool(case.history and conversation.completed_turns),
                "usefulness_score": (human_scores or {}).get(case.id),
                "reference_date": turn.reference_date or case.reference_date,
            }
        )
        results.append(evaluate_quality_case(config, case, replay))

    return summarize_quality_results(
        "live",
        results,
        versions=_evaluation_versions(config, path, cases),
        governance=governance,
    )


async def _run_live_turn(
    question: str,
    *,
    config: AgentConfig,
    bigquery: AnalyticsGateway,
    golden_store: GoldenExampleRepository,
    logger: EventLogger,
    conversation: ConversationState,
    analysis_agent: AnalysisAgentPort,
    user_id: str,
    max_safe_attempts: int,
    retry_delay_seconds: float,
) -> TurnResult:
    attempts = max(1, max_safe_attempts)
    for attempt in range(attempts):
        turn = await run_question(
            question,
            config=config,
            bigquery=bigquery,
            golden_store=golden_store,
            logger=logger,
            user_id=user_id,
            conversation=conversation,
            analysis_agent=analysis_agent,
        )
        safe_to_retry = (
            isinstance(turn.response, AgentFailure)
            and turn.response.retryable
            and turn.query_result is None
            and not turn.sql_tool_invoked
            and attempt + 1 < attempts
        )
        if not safe_to_retry:
            return turn
        await asyncio.sleep(retry_delay_seconds * (2**attempt))
    raise AssertionError("bounded live turn loop returned no result")


def evaluate_quality_case(
    config: AgentConfig, case: QualityEvalCase, replay: QualityReplay
) -> QualityEvalResult:
    intent_assessment = (
        _intent_assessment(
            config,
            replay.candidate_sql,
            case.canonical_sql,
            case.expectations,
        )
        if "intent" in case.evaluators
        else None
    )
    intent = intent_assessment.score if intent_assessment is not None else None
    result_assessment = (
        _row_assessment(
            replay.candidate_rows,
            replay.canonical_rows,
            case.result_contract,
        )
        if "calculation" in case.evaluators
        else None
    )
    calculation = result_assessment.score if result_assessment is not None else None
    retrieval, retrieval_mrr = (
        _retrieval_scores(replay.retrieved_ids, case.expectations.expected_retrieval_ids)
        if "retrieval" in case.evaluators
        else (None, None)
    )
    evidence = assess_report_evidence(
        replay.report,
        replay.candidate_rows,
        replay.candidate_sql,
        case.result_contract.numeric_tolerance,
        reference_date=replay.reference_date,
    )
    unsupported_qualitative_claims = _answer_contract_violations(
        replay.report, case.answer_contract
    )
    faithfulness = (
        evidence.score if not unsupported_qualitative_claims else 0.0
    ) if "faithfulness" in case.evaluators else None
    unsupported_claims = list(evidence.unsupported_numeric_claims)
    multi_turn = (
        1.0 if replay.history_used and intent == 1.0 else 0.0
    ) if "multi_turn" in case.evaluators else None
    usefulness = (
        replay.usefulness_score / 5
        if "usefulness" in case.evaluators and replay.usefulness_score is not None
        else None
    )
    needs_human_review = "usefulness" in case.evaluators and usefulness is None
    executed_sql_attached = report_uses_verified_sql(
        replay.report,
        replay.candidate_sql,
    )
    automated_passed = (
        _applicable_score_passes("intent", intent)
        and _applicable_score_passes("calculation", calculation)
        and _applicable_score_passes("retrieval", retrieval)
        and _applicable_score_passes("retrieval_mrr", retrieval_mrr)
        and _applicable_score_passes("faithfulness", faithfulness)
        and _applicable_score_passes("multi_turn", multi_turn)
        and executed_sql_attached
        and not replay.report.degraded
        and not replay.report.refused
    )
    passed = (
        automated_passed
        and _applicable_score_passes("usefulness", usefulness)
        and not needs_human_review
    )
    scores = QualityScores(
        intent=intent,
        calculation=calculation,
        retrieval=retrieval,
        retrieval_mrr=retrieval_mrr,
        faithfulness=faithfulness,
        multi_turn=multi_turn,
        usefulness=usefulness,
    )
    usefulness_detail = (
        f"{usefulness:.2f}"
        if usefulness is not None
        else "pending"
        if "usefulness" in case.evaluators
        else "n/a"
    )
    report_status = (
        "degraded" if replay.report.degraded else "refused" if replay.report.refused else "complete"
    )
    failed_constraints = (
        [constraint.name for constraint in intent_assessment.constraints if not constraint.passed]
        if intent_assessment is not None
        else []
    )
    detail = (
        f"intent={_format_score(intent)}, calculation={_format_score(calculation)}, "
        f"recall@3={_format_score(retrieval)}, mrr={_format_score(retrieval_mrr)}, "
        f"faithfulness={_format_score(faithfulness)}, "
        f"multi_turn={_format_score(multi_turn)}, usefulness={usefulness_detail}, "
        f"report={report_status}, "
        f"sql_source={'verified' if executed_sql_attached else 'unverified'}, "
        f"failed_constraints={','.join(failed_constraints) if failed_constraints else 'none'}"
    )
    return QualityEvalResult(
        name=case.id,
        suite=case.suite,
        category=case.category,
        risk=case.risk,
        evaluators=case.evaluators,
        passed=passed,
        automated_passed=automated_passed,
        scores=scores,
        detail=detail,
        needs_human_review=needs_human_review,
        critical=case.critical,
        diagnostics=QualityDiagnostics(
            unsupported_numeric_claims=unsupported_claims,
            candidate_sql=replay.candidate_sql,
            candidate_rows=replay.candidate_rows,
            canonical_rows=replay.canonical_rows,
            report_answer=replay.report.answer,
            report_highlights=replay.report.highlights,
            retrieved_ids=replay.retrieved_ids,
            history_used=replay.history_used,
            report_degraded=replay.report.degraded,
            report_refused=replay.report.refused,
            reference_date=replay.reference_date,
            constraint_results=(
                intent_assessment.constraints if intent_assessment is not None else []
            ),
            result_contract_violations=(
                result_assessment.violations if result_assessment is not None else []
            ),
            unsupported_qualitative_claims=unsupported_qualitative_claims,
            verified_sql_attached=executed_sql_attached,
        ),
    )


def summarize_quality_results(
    mode: QualityMode,
    results: list[QualityEvalResult],
    *,
    versions: EvaluationVersions | None = None,
    governance: DatasetGovernance | None = None,
) -> QualitySuiteResult:
    if not results:
        return QualitySuiteResult(
            mode=mode,
            passed=False,
            automated_passed=False,
            results=[],
            aggregate=_zero_scores(),
            case_count=0,
            versions=versions or EvaluationVersions(),
            governance=governance or DatasetGovernance(),
            needs_human_review=False,
        )

    aggregate = QualityScores(
        **{
            score_name: _applicable_mean(results, score_name)
            for score_name in QualityScores.model_fields
        }
    )
    needs_human_review = any(result.needs_human_review for result in results)
    automated_passed = all(result.automated_passed for result in results)
    passed = (
        automated_passed
        and _applicable_score_passes("usefulness", aggregate.usefulness)
        and not needs_human_review
        and all(result.passed for result in results)
    )
    score_summaries = {
        score_name: _metric_summary(results, score_name)
        for score_name in QualityScores.model_fields
    }
    return QualitySuiteResult(
        mode=mode,
        passed=passed,
        automated_passed=automated_passed,
        results=results,
        aggregate=aggregate,
        case_count=len(results),
        suite_counts=dict(sorted(Counter(result.suite for result in results).items())),
        category_counts=dict(sorted(Counter(result.category for result in results).items())),
        risk_counts=dict(sorted(Counter(result.risk for result in results).items())),
        metrics=score_summaries,
        critical_failures=[
            result.name for result in results if result.critical and not result.automated_passed
        ],
        versions=versions or EvaluationVersions(),
        governance=governance or DatasetGovernance(),
        needs_human_review=needs_human_review,
    )


def write_quality_report(result: QualitySuiteResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")


def load_human_scores(path: Path | None) -> dict[str, float]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(name): float(score) for name, score in raw.items()}


def _answer_contract_violations(
    report: AnalysisReport, contract: AnswerContract
) -> list[str]:
    narrative = "\n".join(
        [
            report.answer,
            *report.highlights,
            *report.assumptions,
            *report.caveats,
            *report.followups,
        ]
    )
    normalized = " ".join(narrative.casefold().split())
    violations = [
        f"missing_required_fact:{fact}"
        for fact in contract.required_facts
        if " ".join(fact.casefold().split()) not in normalized
    ]
    for pattern in contract.forbidden_claims:
        try:
            matched = re.search(pattern, narrative, flags=re.IGNORECASE) is not None
        except re.error:
            matched = pattern.casefold() in narrative.casefold()
        if matched:
            violations.append(f"forbidden_claim:{pattern}")
    return violations


def _intent_score(
    config: AgentConfig,
    sql: str,
    canonical_sql: str,
    expectations: QualityExpectations,
) -> float:
    return _intent_assessment(config, sql, canonical_sql, expectations).score


def _intent_assessment(
    config: AgentConfig,
    sql: str,
    canonical_sql: str,
    expectations: QualityExpectations,
) -> IntentAssessment:
    try:
        validation = validate_and_prepare_sql(sql, config)
        canonical_validation = validate_and_prepare_sql(canonical_sql, config)
        candidate_expression = sqlglot.parse_one(validation.safe_sql, read="bigquery")
        canonical_expression = sqlglot.parse_one(canonical_validation.safe_sql, read="bigquery")
        candidate_signature = _intent_signature(candidate_expression)
        canonical_signature = _intent_signature(canonical_expression)
    except (ValueError, SqlglotError) as exc:
        return IntentAssessment(
            score=0.0,
            constraints=[
                ConstraintDiagnostic(
                    name="safe_sql",
                    passed=False,
                    detail=f"{exc.__class__.__name__}: candidate SQL could not be validated",
                )
            ],
        )
    normalized = " ".join(validation.safe_sql.lower().split())
    actual_tables = set(validation.tables)
    structure_ok = candidate_signature.satisfies(canonical_signature)
    joins_ok = _joins_are_allowed(candidate_expression, expectations.allowed_joins)
    constraints = [
        ConstraintDiagnostic(name="safe_sql", passed=True, detail="candidate SQL is safe"),
        *[
            ConstraintDiagnostic(
                name=f"required_table:{table}",
                passed=table in actual_tables,
                detail=("present" if table in actual_tables else "missing"),
            )
            for table in expectations.required_tables
        ],
        *[
            ConstraintDiagnostic(
                name=f"required_sql:{fragment}",
                passed=fragment.lower() in normalized,
                detail=("present" if fragment.lower() in normalized else "missing"),
            )
            for fragment in expectations.required_sql_fragments
        ],
        *[
            ConstraintDiagnostic(
                name=f"forbidden_sql:{fragment}",
                passed=fragment.lower() not in normalized,
                detail=("absent" if fragment.lower() not in normalized else "present"),
            )
            for fragment in expectations.forbidden_sql_fragments
        ],
        ConstraintDiagnostic(
            name="semantic_structure",
            passed=structure_ok,
            detail="matches canonical intent" if structure_ok else "differs from canonical intent",
        ),
        ConstraintDiagnostic(
            name="allowed_joins",
            passed=joins_ok,
            detail="join keys allowed" if joins_ok else "missing or disallowed join key",
        ),
    ]
    return IntentAssessment(
        score=1.0 if all(constraint.passed for constraint in constraints) else 0.0,
        constraints=constraints,
    )


def _row_score(
    candidate_rows: list[dict[str, Any]],
    canonical_rows: list[dict[str, Any]],
    tolerance: float,
    contract: ResultContract | None = None,
) -> float:
    if contract is None:
        contract = ResultContract(
            column_mapping={},
            numeric_tolerance=tolerance,
        )
    return _row_assessment(candidate_rows, canonical_rows, contract).score


def _row_assessment(
    candidate_rows: list[dict[str, Any]],
    canonical_rows: list[dict[str, Any]],
    contract: ResultContract,
) -> ResultAssessment:
    if not canonical_rows:
        return ResultAssessment(
            score=1.0 if not candidate_rows else 0.0,
            violations=[] if not candidate_rows else ["expected_empty_result"],
        )
    violations = _result_contract_violations(candidate_rows, contract)
    if contract.ordered:
        matched = sum(
            _rows_equal(candidate, expected, contract.numeric_tolerance, contract)
            for candidate, expected in zip(candidate_rows, canonical_rows, strict=False)
        )
        if len(candidate_rows) != len(canonical_rows):
            violations.append("row_count_mismatch")
        score = matched / max(len(canonical_rows), len(candidate_rows))
        return ResultAssessment(score=score if not violations else 0.0, violations=violations)
    matched = 0
    unused = list(candidate_rows)
    for row_index, expected in enumerate(canonical_rows):
        match_index = next(
            (
                index
                for index, candidate in enumerate(unused)
                if _rows_equal(
                    candidate,
                    expected,
                    contract.numeric_tolerance,
                    contract,
                )
            ),
            None,
        )
        if match_index is not None:
            matched += 1
            unused.pop(match_index)
        else:
            violations.append(f"unmatched_canonical_row:{row_index}")
    if unused:
        violations.append(f"extra_candidate_rows:{len(unused)}")
    score = matched / max(len(canonical_rows), len(candidate_rows))
    return ResultAssessment(
        score=score if not _has_contract_type_violation(violations) else 0.0,
        violations=violations,
    )


def _rows_equal(
    candidate: dict[str, Any],
    expected: dict[str, Any],
    tolerance: float,
    contract: ResultContract | None = None,
) -> bool:
    if len(candidate) < len(expected):
        return False
    unused = list(candidate.items())
    for expected_key, expected_value in expected.items():
        mapped_key = contract.column_mapping.get(expected_key) if contract is not None else None
        if mapped_key is not None:
            if mapped_key not in candidate:
                return False
            if not _values_equal(candidate[mapped_key], expected_value, tolerance):
                return False
            unused = [(key, value) for key, value in unused if key != mapped_key]
            continue
        exact_index = next(
            (index for index, (key, _) in enumerate(unused) if key == expected_key),
            None,
        )
        if exact_index is not None:
            _, candidate_value = unused.pop(exact_index)
            if not _values_equal(candidate_value, expected_value, tolerance):
                return False
            continue

        value_index = next(
            (
                index
                for index, (candidate_key, candidate_value) in enumerate(unused)
                if _column_names_compatible(candidate_key, expected_key)
                if _values_equal(candidate_value, expected_value, tolerance)
            ),
            None,
        )
        if value_index is None:
            return False
        unused.pop(value_index)
    return True


def _result_contract_violations(
    candidate_rows: list[dict[str, Any]], contract: ResultContract
) -> list[str]:
    violations: list[str] = []
    for row_index, row in enumerate(candidate_rows):
        for canonical_column in contract.key_columns:
            candidate_column = contract.column_mapping[canonical_column]
            if row.get(candidate_column) is None:
                violations.append(f"missing_key:{row_index}:{candidate_column}")
        for canonical_column, unit in contract.units.items():
            candidate_column = contract.column_mapping[canonical_column]
            value = row.get(candidate_column)
            if value is not None and not _unit_value_valid(value, unit):
                violations.append(
                    f"invalid_unit:{row_index}:{candidate_column}:{unit}"
                )
    return violations


def _has_contract_type_violation(violations: list[str]) -> bool:
    return any(
        violation.startswith(("missing_key:", "invalid_unit:")) for violation in violations
    )


def _unit_value_valid(value: Any, unit: ResultUnit) -> bool:
    if unit in {"currency", "percentage"}:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if unit == "count":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            or isinstance(value, float)
            and value.is_integer()
        )
    if unit == "identifier":
        return isinstance(value, (int, str)) and not isinstance(value, bool)
    if unit == "text":
        return isinstance(value, str)
    if unit == "date":
        return isinstance(value, (date, str))
    return False


def _values_equal(candidate: Any, expected: Any, tolerance: float) -> bool:
    if isinstance(candidate, (int, float)) and isinstance(expected, (int, float)):
        return math.isclose(
            float(candidate),
            float(expected),
            rel_tol=tolerance,
            abs_tol=tolerance,
        )
    return candidate == expected


def _column_names_compatible(candidate: str, expected: str) -> bool:
    return bool(_field_tokens(candidate) & _field_tokens(expected))


def _field_tokens(field_name: str) -> set[str]:
    synonyms = {"state": "region"}
    ignored = {"by", "from", "gross", "lost", "to", "total", "value"}
    return {synonyms.get(word, word) for word in _name_words(field_name) if word not in ignored}


def _name_words(name: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", name.lower()))


def _retrieval_scores(retrieved: list[str], expected: list[str]) -> tuple[float, float]:
    if not expected:
        return 1.0, 1.0
    expected_ids = set(expected)
    top_three = retrieved[:3]
    recall = len(set(top_three) & expected_ids) / len(expected_ids)
    first_relevant_rank = next(
        (rank for rank, trio_id in enumerate(top_three, start=1) if trio_id in expected_ids),
        None,
    )
    mrr = 1 / first_relevant_rank if first_relevant_rank is not None else 0.0
    return recall, mrr


def _failed_live_result(case: QualityEvalCase, detail: str) -> QualityEvalResult:
    return QualityEvalResult(
        name=case.id,
        suite=case.suite,
        category=case.category,
        risk=case.risk,
        evaluators=case.evaluators,
        passed=False,
        automated_passed=False,
        scores=QualityScores(
            **{
                score_name: 0.0
                if _score_evaluator(score_name) in case.evaluators
                else None
                for score_name in QualityScores.model_fields
            }
        ),
        detail=detail,
        needs_human_review=False,
        critical=case.critical,
    )


def _zero_scores() -> QualityScores:
    return QualityScores(
        intent=None,
        calculation=None,
        retrieval=None,
        retrieval_mrr=None,
        faithfulness=None,
        multi_turn=None,
        usefulness=None,
    )


def _format_score(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _score_evaluator(score_name: str) -> EvaluatorName:
    return "retrieval" if score_name == "retrieval_mrr" else score_name  # type: ignore[return-value]


def _applicable_score_passes(score_name: ScoreName, value: float | None) -> bool:
    if value is None:
        return True
    thresholds: dict[ScoreName, float] = {
        "intent": 1.0,
        "calculation": 0.95,
        "retrieval": 0.9,
        "retrieval_mrr": 1 / 3,
        "faithfulness": 1.0,
        "multi_turn": 1.0,
        "usefulness": 0.6,
    }
    return value >= thresholds[score_name]


def _applicable_results(
    results: list[QualityEvalResult], score_name: str
) -> list[QualityEvalResult]:
    evaluator = _score_evaluator(score_name)
    return [result for result in results if evaluator in result.evaluators]


def _applicable_mean(results: list[QualityEvalResult], score_name: str) -> float | None:
    values = [
        value
        for result in _applicable_results(results, score_name)
        if (value := getattr(result.scores, score_name)) is not None
    ]
    return mean(values) if values else None


def _metric_summary(
    results: list[QualityEvalResult], score_name: str
) -> MetricSummary:
    applicable = _applicable_results(results, score_name)
    values = [
        value
        for result in applicable
        if (value := getattr(result.scores, score_name)) is not None
    ]
    return MetricSummary(
        applicable_cases=len(applicable),
        passed_cases=sum(
            _applicable_score_passes(score_name, value)  # type: ignore[arg-type]
            for value in values
        ),
        minimum=min(values) if values else None,
        mean=mean(values) if values else None,
        variance=pvariance(values) if len(values) > 1 else 0.0 if values else None,
    )


def _evaluation_versions(
    config: AgentConfig,
    path: Path,
    cases: list[QualityEvalCase],
) -> EvaluationVersions:
    from retail_agent import __version__

    return EvaluationVersions(
        application=__version__,
        dataset_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        prompt=build_analysis_prompt().version,
        persona=config.persona_version,
        model=config.model.llm_model,
        embedding_model=config.model.embedding_model,
        golden_index=config.retrieval.collection,
        reference_dates=sorted({case.reference_date for case in cases}),
    )


@dataclass(frozen=True)
class _SQLIntentSignature:
    aggregates: Counter[str]
    dimensions: frozenset[str]
    filter_columns: frozenset[str]
    filter_literals: frozenset[str]
    functions: frozenset[str]
    time_offsets: Counter[tuple[str, float]]
    date_parts: frozenset[str]
    has_group: bool
    has_having: bool

    def satisfies(self, expected: _SQLIntentSignature) -> bool:
        return (
            expected.aggregates <= self.aggregates
            and expected.dimensions.issubset(self.dimensions)
            and expected.filter_columns.issubset(self.filter_columns)
            and expected.filter_literals.issubset(self.filter_literals)
            and expected.functions.issubset(self.functions)
            and expected.time_offsets <= self.time_offsets
            and expected.date_parts.issubset(self.date_parts)
            and self.has_group == expected.has_group
            and self.has_having == expected.has_having
        )


def _intent_signature(sql: str | exp.Expression) -> _SQLIntentSignature:
    expression = sqlglot.parse_one(sql, read="bigquery") if isinstance(sql, str) else sql
    select = expression.find(exp.Select)
    aliases: dict[str, frozenset[str]] = {}
    dimensions: set[str] = set()
    if select is not None:
        for projection in select.expressions:
            column_names = frozenset(
                column.name.lower() for column in projection.find_all(exp.Column) if column.name
            )
            if projection.alias:
                aliases[projection.alias.lower()] = column_names
            if projection.find(exp.AggFunc) is None:
                dimensions.update(column_names)

    group = expression.find(exp.Group)
    if group is not None:
        for group_expression in group.expressions:
            for column in group_expression.find_all(exp.Column):
                name = column.name.lower()
                dimensions.update(aliases.get(name, {name}))

    predicates = [
        node for node in (expression.find(exp.Where), expression.find(exp.Having)) if node
    ]
    filter_columns = {
        column.name.lower()
        for predicate in predicates
        for column in predicate.find_all(exp.Column)
        if column.name
    }
    filter_literals = {
        literal.this.lower() if isinstance(literal.this, str) else str(literal.this)
        for predicate in predicates
        for literal in predicate.find_all(exp.Literal)
        if not _is_temporal_literal(literal)
    }
    aggregates = Counter(
        _aggregate_signature(aggregate) for aggregate in expression.find_all(exp.AggFunc)
    )
    functions = frozenset(
        _function_signature(function)
        for function in expression.find_all(exp.Func)
        if not isinstance(function, (exp.AggFunc, exp.Case, exp.If, exp.And))
        and not _is_cosmetic_round(function)
    )
    return _SQLIntentSignature(
        aggregates=aggregates,
        dimensions=frozenset(dimensions),
        filter_columns=frozenset(filter_columns),
        filter_literals=frozenset(filter_literals),
        functions=functions,
        time_offsets=_normalized_time_offsets(expression),
        date_parts=frozenset(_date_part(node) for node in expression.find_all(exp.DateTrunc)),
        has_group=group is not None,
        has_having=expression.find(exp.Having) is not None,
    )


def _aggregate_signature(aggregate: exp.AggFunc) -> str:
    columns = sorted(
        column.name.lower()
        for column in aggregate.find_all(exp.Column)
        if column.name and not _is_conditional_predicate_column(column, aggregate)
    )
    if not columns and aggregate.find(exp.Star) is not None:
        columns = ["*"]
    distinct = ":distinct" if aggregate.find(exp.Distinct) is not None else ""
    return f"{aggregate.sql_name().lower()}:{','.join(columns)}{distinct}"


def _function_signature(function: exp.Func) -> str:
    if isinstance(function, exp.Date):
        return "to_date"
    if isinstance(function, exp.Cast):
        target = function.args.get("to")
        if isinstance(target, exp.DataType) and target.this == exp.DType.DATE:
            return "to_date"
    if isinstance(function, exp.Round):
        decimals = function.args.get("decimals")
        precision = decimals.sql(dialect="bigquery") if decimals is not None else "0"
        return f"round:{precision.lower()}"
    return function.sql_name().lower()


def _is_cosmetic_round(function: exp.Func) -> bool:
    if not isinstance(function, exp.Round) or function.find(exp.AggFunc) is None:
        return False
    parent = function.parent
    if isinstance(parent, exp.Alias):
        alias = parent.alias
        parent = parent.parent
        if isinstance(parent, exp.Select):
            clauses = [
                parent.args.get(name)
                for name in ("group", "having", "order", "qualify", "where")
            ]
            clauses.extend(parent.args.get("joins") or [])
            if any(
                isinstance(clause, exp.Expression)
                and any(
                    column.name.lower() == alias.lower()
                    for column in clause.find_all(exp.Column)
                )
                for clause in clauses
            ):
                return False
    return isinstance(parent, exp.Select)


def _is_conditional_predicate_column(column: exp.Column, aggregate: exp.AggFunc) -> bool:
    child: exp.Expression = column
    parent = column.parent
    while parent is not None and parent is not aggregate:
        if isinstance(parent, exp.If) and parent.this is child:
            return True
        child = parent
        parent = parent.parent
    return False


def _joins_are_allowed(expression: exp.Expression, allowed_joins: list[JoinKey]) -> bool:
    joins = list(expression.find_all(exp.Join))
    if not joins:
        return True

    aliases = normalized_table_aliases(expression)
    allowed = {join.normalized() for join in allowed_joins}
    for join in joins:
        join_keys = {
            key
            for equality in join.find_all(exp.EQ)
            if (key := _join_key(equality, aliases)) is not None
        }
        if not join_keys or not join_keys.issubset(allowed):
            return False
    return True


def _join_key(equality: exp.EQ, aliases: dict[str, str]) -> tuple[str, str] | None:
    if not isinstance(equality.this, exp.Column) or not isinstance(equality.expression, exp.Column):
        return None
    left = _qualified_join_column(equality.this, aliases)
    right = _qualified_join_column(equality.expression, aliases)
    if left is None or right is None or left.split(".", 1)[0] == right.split(".", 1)[0]:
        return None
    return tuple(sorted((left, right)))


def _qualified_join_column(column: exp.Column, aliases: dict[str, str]) -> str | None:
    if not column.table or not column.name:
        return None
    table = aliases.get(column.table.lower())
    if table is None:
        return None
    return f"{table}.{column.name.lower()}"


def _normalized_time_offsets(expression: exp.Expression) -> Counter[tuple[str, float]]:
    offsets: Counter[tuple[str, float]] = Counter()
    for function_type in (exp.DateAdd, exp.DateSub):
        for function in expression.find_all(function_type):
            magnitude = function.args.get("expression")
            unit = function.args.get("unit")
            if isinstance(magnitude, exp.Literal) and unit is not None:
                normalized = _normalize_duration(float(magnitude.this), unit.name)
                if normalized is not None:
                    offsets[normalized] += 1
    return offsets


def _normalize_duration(value: float, unit: str) -> tuple[str, float] | None:
    normalized_unit = unit.lower()
    month_factors = {"month": 1, "quarter": 3, "year": 12}
    day_factors = {"day": 1, "week": 7}
    if normalized_unit in month_factors:
        return "months", value * month_factors[normalized_unit]
    if normalized_unit in day_factors:
        return "days", value * day_factors[normalized_unit]
    return None


def _is_temporal_literal(literal: exp.Literal) -> bool:
    parent = literal.parent
    return (isinstance(parent, (exp.DateAdd, exp.DateSub)) and literal.arg_key == "expression") or (
        isinstance(parent, exp.DateTrunc) and literal.arg_key == "unit"
    )


def _date_part(date_trunc: exp.DateTrunc) -> str:
    unit = date_trunc.args.get("unit")
    return unit.name.lower() if unit is not None else ""
