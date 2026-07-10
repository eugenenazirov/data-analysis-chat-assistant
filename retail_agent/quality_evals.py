from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Literal

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp
from sqlglot.errors import SqlglotError

from retail_agent.agent import ConversationState, TurnResult, run_question
from retail_agent.config import AgentConfig
from retail_agent.models import AgentFailure, AnalysisReport
from retail_agent.observability import EventLogger, new_trace_id
from retail_agent.ports import AnalysisAgentPort, KnowledgeRetrieverPort, WarehousePort
from retail_agent.sql_guard import validate_and_prepare_sql

NUMBER_PATTERN = re.compile(
    r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:%|[KMBkmb]|thousand|million|billion))?",
    re.IGNORECASE,
)
MAX_DERIVATION_VALUES_PER_MEASURE = 100
type QualityMode = Literal["replay", "live"]


class QualityExpectations(BaseModel):
    required_tables: list[str] = Field(default_factory=list)
    required_sql_fragments: list[str] = Field(default_factory=list)
    forbidden_sql_fragments: list[str] = Field(default_factory=list)
    expected_retrieval_ids: list[str] = Field(default_factory=list)
    numeric_tolerance: float = 0.001


class QualityReplay(BaseModel):
    candidate_sql: str
    candidate_rows: list[dict[str, Any]]
    canonical_rows: list[dict[str, Any]]
    retrieved_ids: list[str]
    report: AnalysisReport
    history_used: bool = False
    usefulness_score: float | None = Field(default=None, ge=0, le=5)


class QualityEvalCase(BaseModel):
    id: str
    question: str
    user_id: str = "manager_a"
    history: list[str] = Field(default_factory=list)
    canonical_sql: str
    expectations: QualityExpectations
    replay: QualityReplay
    critical: bool = False


class QualityScores(BaseModel):
    intent: float
    calculation: float
    retrieval: float
    retrieval_mrr: float
    faithfulness: float
    multi_turn: float
    usefulness: float | None


class QualityDiagnostics(BaseModel):
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


class QualityEvalResult(BaseModel):
    name: str
    passed: bool
    automated_passed: bool
    scores: QualityScores
    detail: str
    needs_human_review: bool = False
    critical: bool = False
    diagnostics: QualityDiagnostics | None = None


class QualitySuiteResult(BaseModel):
    mode: QualityMode
    passed: bool
    automated_passed: bool
    results: list[QualityEvalResult]
    aggregate: QualityScores
    needs_human_review: bool = False


def load_quality_cases(path: Path) -> list[QualityEvalCase]:
    cases: list[QualityEvalCase] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                cases.append(QualityEvalCase.model_validate(json.loads(line)))
    return cases


def run_quality_replay_evals(config: AgentConfig, path: Path) -> QualitySuiteResult:
    cases = load_quality_cases(path)
    results = [evaluate_quality_case(config, case, case.replay) for case in cases]
    return summarize_quality_results("replay", results)


async def run_quality_live_evals(
    config: AgentConfig,
    path: Path,
    *,
    bigquery: WarehousePort,
    golden_store: KnowledgeRetrieverPort,
    logger: EventLogger,
    analysis_agent: AnalysisAgentPort,
    human_scores: dict[str, float] | None = None,
    max_safe_attempts: int = 3,
    retry_delay_seconds: float = 5.0,
) -> QualitySuiteResult:
    results: list[QualityEvalResult] = []
    for case in load_quality_cases(path):
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
                _failed_live_result(case.id, "history turn failed", critical=case.critical)
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
                _failed_live_result(
                    case.id, "candidate agent run failed", critical=case.critical
                )
            )
            continue

        try:
            canonical = bigquery.execute(case.canonical_sql, new_trace_id())
        except Exception as exc:
            results.append(
                _failed_live_result(
                    case.id,
                    f"canonical query failed: {exc.__class__.__name__}",
                    critical=case.critical,
                )
            )
            continue

        replay = QualityReplay(
            candidate_sql=turn.query_result.sql,
            candidate_rows=turn.query_result.rows,
            canonical_rows=canonical.rows,
            retrieved_ids=list(turn.retrieved_trio_ids),
            report=turn.response,
            history_used=not case.history or bool(conversation.completed_turns),
            usefulness_score=(human_scores or {}).get(case.id),
        )
        results.append(evaluate_quality_case(config, case, replay))

    return summarize_quality_results("live", results)


async def _run_live_turn(
    question: str,
    *,
    config: AgentConfig,
    bigquery: WarehousePort,
    golden_store: KnowledgeRetrieverPort,
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
    intent = _intent_score(
        config,
        replay.candidate_sql,
        case.canonical_sql,
        case.expectations,
    )
    calculation = _row_score(
        replay.candidate_rows,
        replay.canonical_rows,
        case.expectations.numeric_tolerance,
    )
    retrieval, retrieval_mrr = _retrieval_scores(
        replay.retrieved_ids, case.expectations.expected_retrieval_ids
    )
    faithfulness, unsupported_claims = _faithfulness_details(
        replay.report,
        replay.candidate_rows,
        replay.candidate_sql,
        case.expectations.numeric_tolerance,
    )
    multi_turn = (
        1.0 if not case.history else 1.0 if replay.history_used and intent == 1.0 else 0.0
    )
    usefulness = (
        replay.usefulness_score / 5 if replay.usefulness_score is not None else None
    )
    needs_human_review = usefulness is None
    executed_sql_attached = replay.report.sql == replay.candidate_sql
    automated_passed = (
        intent == 1.0
        and calculation >= 0.95
        and retrieval >= 0.9
        and retrieval_mrr >= 1 / 3
        and faithfulness == 1.0
        and multi_turn == 1.0
        and executed_sql_attached
        and not replay.report.degraded
        and not replay.report.refused
    )
    passed = automated_passed and usefulness is not None and usefulness >= 0.6
    scores = QualityScores(
        intent=intent,
        calculation=calculation,
        retrieval=retrieval,
        retrieval_mrr=retrieval_mrr,
        faithfulness=faithfulness,
        multi_turn=multi_turn,
        usefulness=usefulness,
    )
    usefulness_detail = f"{usefulness:.2f}" if usefulness is not None else "pending"
    report_status = (
        "degraded" if replay.report.degraded else "refused" if replay.report.refused else "complete"
    )
    detail = (
        f"intent={intent:.2f}, calculation={calculation:.2f}, "
        f"recall@3={retrieval:.2f}, mrr={retrieval_mrr:.2f}, "
        f"faithfulness={faithfulness:.2f}, "
        f"multi_turn={multi_turn:.2f}, usefulness={usefulness_detail}, "
        f"report={report_status}, "
        f"sql_source={'verified' if executed_sql_attached else 'unverified'}"
    )
    return QualityEvalResult(
        name=case.id,
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
        ),
    )


def summarize_quality_results(
    mode: QualityMode, results: list[QualityEvalResult]
) -> QualitySuiteResult:
    if not results:
        return QualitySuiteResult(
            mode=mode,
            passed=False,
            automated_passed=False,
            results=[],
            aggregate=_zero_scores(),
            needs_human_review=False,
        )

    usefulness_scores = [
        result.scores.usefulness
        for result in results
        if result.scores.usefulness is not None
    ]
    aggregate = QualityScores(
        intent=mean(result.scores.intent for result in results),
        calculation=mean(result.scores.calculation for result in results),
        retrieval=mean(result.scores.retrieval for result in results),
        retrieval_mrr=mean(result.scores.retrieval_mrr for result in results),
        faithfulness=mean(result.scores.faithfulness for result in results),
        multi_turn=mean(result.scores.multi_turn for result in results),
        usefulness=mean(usefulness_scores) if len(usefulness_scores) == len(results) else None,
    )
    needs_human_review = any(result.needs_human_review for result in results)
    automated_passed = (
        aggregate.intent >= 0.95
        and aggregate.calculation >= 0.95
        and aggregate.retrieval >= 0.9
        and aggregate.retrieval_mrr >= 0.8
        and aggregate.faithfulness == 1.0
        and aggregate.multi_turn >= 0.9
        and all(result.automated_passed for result in results)
    )
    passed = (
        automated_passed
        and aggregate.usefulness is not None
        and aggregate.usefulness >= 0.8
        and not needs_human_review
        and all(result.passed for result in results)
    )
    return QualitySuiteResult(
        mode=mode,
        passed=passed,
        automated_passed=automated_passed,
        results=results,
        aggregate=aggregate,
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


def _intent_score(
    config: AgentConfig,
    sql: str,
    canonical_sql: str,
    expectations: QualityExpectations,
) -> float:
    try:
        validation = validate_and_prepare_sql(sql, config)
        canonical_validation = validate_and_prepare_sql(canonical_sql, config)
        candidate_signature = _intent_signature(validation.safe_sql)
        canonical_signature = _intent_signature(canonical_validation.safe_sql)
    except (ValueError, SqlglotError):
        return 0.0
    normalized = " ".join(validation.safe_sql.lower().split())
    required_tables = set(expectations.required_tables)
    tables_ok = required_tables.issubset(set(validation.tables))
    fragments_ok = all(
        fragment.lower() in normalized
        for fragment in expectations.required_sql_fragments
    )
    forbidden_ok = all(
        fragment.lower() not in normalized
        for fragment in expectations.forbidden_sql_fragments
    )
    structure_ok = candidate_signature.satisfies(canonical_signature)
    return 1.0 if tables_ok and fragments_ok and forbidden_ok and structure_ok else 0.0


def _row_score(
    candidate_rows: list[dict[str, Any]],
    canonical_rows: list[dict[str, Any]],
    tolerance: float,
) -> float:
    if not canonical_rows:
        return 1.0 if not candidate_rows else 0.0
    matched = 0
    unused = list(candidate_rows)
    for expected in canonical_rows:
        match_index = next(
            (
                index
                for index, candidate in enumerate(unused)
                if _rows_equal(candidate, expected, tolerance)
            ),
            None,
        )
        if match_index is not None:
            matched += 1
            unused.pop(match_index)
    return matched / len(canonical_rows)


def _rows_equal(
    candidate: dict[str, Any], expected: dict[str, Any], tolerance: float
) -> bool:
    if len(candidate) != len(expected):
        return False
    if set(candidate) == set(expected):
        pairs = [(candidate[key], expected[key]) for key in expected]
    else:
        pairs = list(zip(candidate.values(), expected.values(), strict=True))
    for candidate_value, expected_value in pairs:
        if isinstance(candidate_value, (int, float)) and isinstance(
            expected_value, (int, float)
        ):
            if not math.isclose(
                float(candidate_value),
                float(expected_value),
                rel_tol=tolerance,
                abs_tol=tolerance,
            ):
                return False
        elif candidate_value != expected_value:
            return False
    return True


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


def _faithfulness_score(
    report: AnalysisReport,
    rows: list[dict[str, Any]],
    sql: str,
    tolerance: float,
) -> float:
    return _faithfulness_details(report, rows, sql, tolerance)[0]


def _faithfulness_details(
    report: AnalysisReport,
    rows: list[dict[str, Any]],
    sql: str,
    tolerance: float,
) -> tuple[float, list[float]]:
    text = " ".join([report.answer, *report.highlights])
    claims = [_parse_number(match.group()) for match in NUMBER_PATTERN.finditer(text)]
    if not claims:
        return 1.0, []
    measures: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for column, value in row.items():
            if isinstance(value, (int, float)):
                measures[column].append(float(value))
    context_values = _supported_context_numbers(sql)
    if not measures and not context_values:
        return 0.0, claims
    unsupported = [
        claim
        for claim in claims
        if not _claim_supported(
            claim,
            measures,
            context_values,
            tolerance,
        )
    ]
    return (len(claims) - len(unsupported)) / len(claims), unsupported


def _supported_context_numbers(sql: str) -> list[float]:
    values = [
        float(raw)
        for raw in re.findall(r"\b(?:LIMIT|INTERVAL)\s+(\d+)\b", sql, re.IGNORECASE)
    ]
    if "CURRENT_DATE" in sql.upper():
        today = datetime.now(UTC).date()
        previous_month = today.replace(day=1) - timedelta(days=1)
        values.extend(
            [
                float(today.year),
                float(today.month),
                float(previous_month.year),
                float(previous_month.month),
            ]
        )
    return values


def _claim_supported(
    claim: float,
    measures: dict[str, list[float]],
    context_values: list[float],
    tolerance: float,
) -> bool:
    raw_values = [value for values in measures.values() for value in values]
    if any(_numbers_match(claim, value, tolerance) for value in [*raw_values, *context_values]):
        return True

    for values in measures.values():
        bounded = values[:MAX_DERIVATION_VALUES_PER_MEASURE]
        for left_index, left in enumerate(bounded):
            for right in bounded[left_index + 1 :]:
                derived = [left - right, right - left, abs(left - right)]
                if right:
                    derived.extend(
                        [
                            left / right,
                            left / right * 100,
                            (left - right) / right * 100,
                        ]
                    )
                if left:
                    derived.extend(
                        [
                            right / left,
                            right / left * 100,
                            (right - left) / left * 100,
                        ]
                    )
                if any(_numbers_match(claim, value, tolerance) for value in derived):
                    return True
    return False


def _numbers_match(claim: float, value: float, tolerance: float) -> bool:
    relative_tolerance = max(tolerance, 0.005)
    return math.isclose(
        claim,
        value,
        rel_tol=relative_tolerance,
        abs_tol=tolerance,
    ) or math.isclose(
        claim / 100,
        value,
        rel_tol=relative_tolerance,
        abs_tol=tolerance,
    )


def _parse_number(raw: str) -> float:
    normalized = raw.strip().lower().replace(",", "")
    multipliers = {
        "k": 1_000,
        "thousand": 1_000,
        "m": 1_000_000,
        "million": 1_000_000,
        "b": 1_000_000_000,
        "billion": 1_000_000_000,
    }
    for suffix, multiplier in multipliers.items():
        if normalized.endswith(suffix):
            return float(normalized[: -len(suffix)].strip()) * multiplier
    return float(normalized.rstrip("%").strip())


def _failed_live_result(
    name: str, detail: str, *, critical: bool = False
) -> QualityEvalResult:
    return QualityEvalResult(
        name=name,
        passed=False,
        automated_passed=False,
        scores=_zero_scores(),
        detail=detail,
        needs_human_review=False,
        critical=critical,
    )


def _zero_scores() -> QualityScores:
    return QualityScores(
        intent=0,
        calculation=0,
        retrieval=0,
        retrieval_mrr=0,
        faithfulness=0,
        multi_turn=0,
        usefulness=None,
    )


@dataclass(frozen=True)
class _SQLIntentSignature:
    aggregates: Counter[str]
    dimensions: frozenset[str]
    filter_columns: frozenset[str]
    filter_literals: frozenset[str]
    functions: frozenset[str]
    joins_well_formed: bool
    has_group: bool
    has_having: bool

    def satisfies(self, expected: _SQLIntentSignature) -> bool:
        return (
            self.aggregates == expected.aggregates
            and expected.dimensions.issubset(self.dimensions)
            and expected.filter_columns.issubset(self.filter_columns)
            and expected.filter_literals.issubset(self.filter_literals)
            and expected.functions.issubset(self.functions)
            and self.joins_well_formed
            and self.has_group == expected.has_group
            and self.has_having == expected.has_having
        )


def _intent_signature(sql: str) -> _SQLIntentSignature:
    expression = sqlglot.parse_one(sql, read="bigquery")
    select = expression.find(exp.Select)
    aliases: dict[str, frozenset[str]] = {}
    dimensions: set[str] = set()
    if select is not None:
        for projection in select.expressions:
            column_names = frozenset(
                column.name.lower()
                for column in projection.find_all(exp.Column)
                if column.name
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
        node
        for node in (expression.find(exp.Where), expression.find(exp.Having))
        if node
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
    }
    aggregates = Counter(
        _aggregate_signature(aggregate)
        for aggregate in expression.find_all(exp.AggFunc)
    )
    functions = frozenset(
        _function_signature(function)
        for function in expression.find_all(exp.Func)
        if not isinstance(function, (exp.AggFunc, exp.Case, exp.If, exp.And))
    )
    joins = list(expression.find_all(exp.Join))
    joins_well_formed = all(
        any(
            isinstance(equality.this, exp.Column)
            and isinstance(equality.expression, exp.Column)
            and equality.this.table
            and equality.expression.table
            and equality.this.table.lower() != equality.expression.table.lower()
            for equality in join.find_all(exp.EQ)
        )
        for join in joins
    )
    return _SQLIntentSignature(
        aggregates=aggregates,
        dimensions=frozenset(dimensions),
        filter_columns=frozenset(filter_columns),
        filter_literals=frozenset(filter_literals),
        functions=functions,
        joins_well_formed=joins_well_formed,
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
    return function.sql_name().lower()


def _is_conditional_predicate_column(
    column: exp.Column, aggregate: exp.AggFunc
) -> bool:
    child: exp.Expression = column
    parent = column.parent
    while parent is not None and parent is not aggregate:
        if isinstance(parent, exp.If) and parent.this is child:
            return True
        child = parent
        parent = parent.parent
    return False
