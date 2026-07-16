from pathlib import Path

import yaml

CI_PATH = Path(".github/workflows/ci.yml")
LIVE_PATH = Path(".github/workflows/live-quality-eval.yml")
RELEASE_PATH = Path(".github/workflows/release-quality-gate.yml")
DOCKERFILE_PATH = Path("Dockerfile")
COMPOSE_PATH = Path("compose.yaml")
JUSTFILE_PATH = Path("justfile")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_workflows_are_valid_yaml_with_expected_jobs():
    assert set(_load(CI_PATH)["jobs"]) == {"verify"}
    assert set(_load(LIVE_PATH)["jobs"]) == {"evaluate"}
    assert set(_load(RELEASE_PATH)["jobs"]) == {"approve"}


def test_pr_ci_runs_all_offline_suites_and_checks_runtime_image_separation():
    workflow = CI_PATH.read_text(encoding="utf-8")

    for dataset in (
        "release_holdout.jsonl",
        "multi_turn.jsonl",
        "development.jsonl",
        "adversarial.jsonl",
        "regression.jsonl",
    ):
        assert dataset in workflow
    assert "build_replay_fixtures --check" in workflow
    assert "--target runtime" in workflow
    assert "find_spec('pydantic_evals') is None" in workflow
    assert "not pathlib.Path('/app/evals').exists()" in workflow


def test_live_workflow_separates_canary_from_release_candidate():
    workflow = LIVE_PATH.read_text(encoding="utf-8")

    assert 'echo "repetitions=3"' in workflow
    assert 'echo "repetitions=5"' in workflow
    assert "release_holdout.jsonl" in workflow
    assert "human-review-form" in workflow
    assert "human_scores" not in workflow
    assert "candidate-metadata.json" in workflow
    assert "retention-days:" in workflow
    assert "github.ref_name == github.event.repository.default_branch" in workflow
    assert "environment: quality-live-evaluation" in workflow


def test_release_approval_consumes_frozen_run_and_never_reruns_live_queries():
    workflow = RELEASE_PATH.read_text(encoding="utf-8")

    assert "candidate_run_id" in workflow
    assert "gh run download" in workflow
    assert "digest mismatch" in workflow
    assert "release-decision" in workflow
    assert "--mode live" not in workflow
    assert "google-github-actions/auth" not in workflow
    assert "head_branch" in workflow
    assert "DEFAULT_BRANCH" in workflow


def test_evaluation_assets_are_readable_by_the_non_root_image_user():
    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert "COPY --chown=appuser:appuser evals ./evals" in dockerfile


def test_compose_exposes_chart_artifacts_in_the_local_workspace():
    compose = _load(COMPOSE_PATH)

    assert "./artifacts:/app/artifacts" in compose["services"]["app"]["volumes"]
    assert "chart_artifacts" not in compose["volumes"]
    justfile = JUSTFILE_PATH.read_text(encoding="utf-8")
    assert "_live-image: _artifact-directory" in justfile
    assert "chmod a+rwx artifacts artifacts/charts" in justfile
