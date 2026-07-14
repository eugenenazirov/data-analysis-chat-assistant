import json
from pathlib import Path

import pytest

from evals.dataset import (
    inspect_dataset_governance,
    validate_partition_path,
    validate_quality_case_schema,
)
from evals.quality import load_quality_cases

SMOKE_PATH = Path("evals/datasets/smoke.jsonl")
SCHEMA_PATH = Path("evals/datasets/schema/quality-case.schema.json")
GOLDEN_PATH = Path("data/golden_trios.jsonl")


def test_smoke_overlap_is_explicit_and_intentional():
    governance = inspect_dataset_governance(load_quality_cases(SMOKE_PATH), GOLDEN_PATH)

    assert governance.intentional_overlap_count == 3
    assert governance.golden_question_overlap_ids == [
        "customer_spend_pii_safe_critical",
        "monthly_revenue_category_critical",
        "product_return_risk",
    ]
    assert governance.golden_sql_overlap_ids == governance.golden_question_overlap_ids


def test_release_holdout_rejects_golden_question_or_sql_overlap():
    case = load_quality_cases(SMOKE_PATH)[0].model_copy(update={"suite": "release_holdout"})

    with pytest.raises(ValueError, match="Release holdout overlaps Golden Knowledge"):
        inspect_dataset_governance([case], GOLDEN_PATH)


def test_partition_path_rejects_mismatched_suite():
    case = load_quality_cases(SMOKE_PATH)[0]

    with pytest.raises(ValueError, match="release_holdout dataset partition"):
        validate_partition_path(Path("release_holdout.jsonl"), [case])


def test_fixture_content_mutation_requires_reviewed_hash_refresh(tmp_path):
    raw = json.loads(SMOKE_PATH.read_text(encoding="utf-8").splitlines()[0])
    raw["replay"]["canonical_rows"][0]["revenue"] += 1
    path = tmp_path / "mutated.jsonl"
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="fixture content hash"):
        load_quality_cases(path)


def test_fixture_canonical_sql_hash_must_match(tmp_path):
    raw = json.loads(SMOKE_PATH.read_text(encoding="utf-8").splitlines()[0])
    raw["canonical_sql"] += " "
    path = tmp_path / "mutated-sql.jsonl"
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="canonical SQL hash"):
        load_quality_cases(path)


def test_committed_quality_case_schema_is_current():
    validate_quality_case_schema(SCHEMA_PATH)


def test_stale_quality_case_schema_fails_validation(tmp_path):
    path = tmp_path / "stale-schema.json"
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="schema is stale"):
        validate_quality_case_schema(path)
