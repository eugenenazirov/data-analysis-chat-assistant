from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Literal

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp
from sqlglot.errors import SqlglotError

from retail_agent.agent import ConversationState, TurnResult, run_question
from retail_agent.application.ports import AnalyticsGateway, GoldenExampleRepository
from retail_agent.config import AgentConfig
from retail_agent.domain.policies.report_evidence import (
    assess_report_evidence,
    report_uses_verified_sql,
)
from retail_agent.models import AgentFailure, AnalysisReport
from retail_agent.observability import EventLogger, new_trace_id
from retail_agent.ports import AnalysisAgentPort
from retail_agent.sql_guard import normalized_table_aliases, validate_and_prepare_sql

type QualityMode = Literal["replay", "live"]


class JoinKey(BaseModel):
    left: str
    right: str

    def normalized(self) -> tuple[str, str]:
        return tuple(sorted((self.left.lower(), self.right.lower())))


class QualityExpectations(BaseModel):
    required_tables: list[str] = Field(default_factory=list)
    allowed_joins: list[JoinKey] = Field(default_factory=list)
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
    bigquery: AnalyticsGateway,
    golden_store: GoldenExampleRepository,
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
                _failed_live_result(case.id, "candidate agent run failed", critical=case.critical)
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
    evidence = assess_report_evidence(
        replay.report,
        replay.candidate_rows,
        replay.candidate_sql,
        case.expectations.numeric_tolerance,
    )
    faithfulness = evidence.score
    unsupported_claims = list(evidence.unsupported_numeric_claims)
    multi_turn = 1.0 if not case.history else 1.0 if replay.history_used and intent == 1.0 else 0.0
    usefulness = replay.usefulness_score / 5 if replay.usefulness_score is not None else None
    needs_human_review = usefulness is None
    executed_sql_attached = report_uses_verified_sql(
        replay.report,
        replay.candidate_sql,
    )
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
        result.scores.usefulness for result in results if result.scores.usefulness is not None
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
        candidate_expression = sqlglot.parse_one(validation.safe_sql, read="bigquery")
        canonical_expression = sqlglot.parse_one(canonical_validation.safe_sql, read="bigquery")
        candidate_signature = _intent_signature(candidate_expression)
        canonical_signature = _intent_signature(canonical_expression)
    except (ValueError, SqlglotError):
        return 0.0
    normalized = " ".join(validation.safe_sql.lower().split())
    required_tables = set(expectations.required_tables)
    tables_ok = required_tables.issubset(set(validation.tables))
    fragments_ok = all(
        fragment.lower() in normalized for fragment in expectations.required_sql_fragments
    )
    forbidden_ok = all(
        fragment.lower() not in normalized for fragment in expectations.forbidden_sql_fragments
    )
    structure_ok = candidate_signature.satisfies(canonical_signature)
    joins_ok = _joins_are_allowed(candidate_expression, expectations.allowed_joins)
    return 1.0 if tables_ok and fragments_ok and forbidden_ok and structure_ok and joins_ok else 0.0


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
    return matched / max(len(canonical_rows), len(candidate_rows))


def _rows_equal(candidate: dict[str, Any], expected: dict[str, Any], tolerance: float) -> bool:
    if len(candidate) < len(expected):
        return False
    unused = list(candidate.items())
    for expected_key, expected_value in expected.items():
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


def _failed_live_result(name: str, detail: str, *, critical: bool = False) -> QualityEvalResult:
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
    return function.sql_name().lower()


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
