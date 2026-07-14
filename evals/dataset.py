from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import sqlglot

from evals.quality import DatasetGovernance, EvaluationSuite, QualityEvalCase

_PARTITION_NAMES: dict[str, EvaluationSuite] = {
    "smoke": "smoke",
    "development": "development",
    "regression": "regression",
    "release_holdout": "release_holdout",
    "adversarial": "adversarial",
    "multi_turn": "multi_turn",
}


def validate_partition_path(path: Path, cases: list[QualityEvalCase]) -> None:
    expected_suite = _PARTITION_NAMES.get(path.stem)
    if expected_suite is None:
        return
    mismatches = sorted(case.id for case in cases if case.suite != expected_suite)
    if mismatches:
        raise ValueError(
            f"Cases do not match the {expected_suite} dataset partition: "
            f"{', '.join(mismatches)}"
        )


def inspect_dataset_governance(
    cases: list[QualityEvalCase],
    golden_path: Path,
) -> DatasetGovernance:
    golden = _load_golden_examples(golden_path)
    golden_questions = {_normalize_question(item["question"]) for item in golden}
    golden_sql = {_normalize_sql(item["sql"]) for item in golden}
    question_overlaps = sorted(
        case.id for case in cases if _normalize_question(case.question) in golden_questions
    )
    sql_overlaps = sorted(
        case.id for case in cases if _normalize_sql(case.canonical_sql) in golden_sql
    )
    forbidden_overlap = sorted(
        case.id
        for case in cases
        if case.suite == "release_holdout"
        and case.id in {*question_overlaps, *sql_overlaps}
    )
    if forbidden_overlap:
        raise ValueError(
            "Release holdout overlaps Golden Knowledge: " + ", ".join(forbidden_overlap)
        )
    intentional = {
        case.id
        for case in cases
        if case.suite == "smoke" and case.id in {*question_overlaps, *sql_overlaps}
    }
    return DatasetGovernance(
        golden_question_overlap_ids=question_overlaps,
        golden_sql_overlap_ids=sql_overlaps,
        intentional_overlap_count=len(intentional),
    )


def write_quality_case_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(quality_case_schema_json(), encoding="utf-8")


def quality_case_schema_json() -> str:
    schema = QualityEvalCase.model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def validate_quality_case_schema(path: Path) -> None:
    expected = quality_case_schema_json()
    try:
        actual = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"Evaluation case schema is missing: {path}") from exc
    if actual != expected:
        raise ValueError(
            f"Evaluation case schema is stale: regenerate {path} from the validated model"
        )


def _load_golden_examples(path: Path) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                question = item["question"]
                sql = item["sql"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ValueError(
                    f"Invalid Golden Knowledge entry at {path}:{line_number}"
                ) from exc
            if not isinstance(question, str) or not isinstance(sql, str):
                raise ValueError(f"Invalid Golden Knowledge entry at {path}:{line_number}")
            examples.append(item)
    return examples


def _normalize_question(question: str) -> str:
    return " ".join(re.findall(r"[\w]+", question.casefold()))


def _normalize_sql(sql: str) -> str:
    try:
        return sqlglot.parse_one(sql, read="bigquery").sql(
            dialect="bigquery", pretty=False, normalize=True
        )
    except sqlglot.errors.SqlglotError:
        return " ".join(sql.casefold().split())
