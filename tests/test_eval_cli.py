import json
from pathlib import Path

from typer.testing import CliRunner

from evals import run as cli
from evals.quality import (
    evaluate_quality_case,
    load_quality_cases,
    summarize_quality_results,
    write_quality_report,
)


def test_guardrail_eval_cli_passes():
    result = CliRunner().invoke(cli.app, ["guardrails"])

    assert result.exit_code == 0, result.output


def test_quality_replay_cli_passes():
    result = CliRunner().invoke(cli.app, ["quality", "--mode", "replay"])

    assert result.exit_code == 0, result.output


def test_dataset_validation_cli_reports_intentional_overlap():
    result = CliRunner().invoke(cli.app, ["validate-dataset"])

    assert result.exit_code == 0, result.output
    assert "dataset_valid cases=4" in result.output
    assert "intentional_overlap=3" in result.output


def test_eval_runner_rejects_unknown_command():
    result = CliRunner().invoke(cli.app, ["unknown"])
    help_result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code != 0
    assert "guardrails" in help_result.output
    assert "quality" in help_result.output


def test_quality_automated_only_accepts_pending_human_review(test_config, monkeypatch):
    case = load_quality_cases(cli.DEFAULT_CASES_PATH)[0]
    replay = case.replay.model_copy(update={"usefulness_score": None})
    pending = summarize_quality_results(
        "replay", [evaluate_quality_case(test_config, case, replay)]
    )
    monkeypatch.setattr(cli, "load_settings", lambda path: test_config)
    monkeypatch.setattr(cli, "run_quality_replay_evals", lambda config, path: pending)

    automated = CliRunner().invoke(
        cli.app,
        ["quality", "--mode", "replay", "--automated-only"],
    )
    release = CliRunner().invoke(
        cli.app,
        ["quality", "--mode", "replay"],
    )

    assert automated.exit_code == 0
    assert "AUTO PASS" in automated.output
    assert release.exit_code == 1


def test_default_quality_dataset_is_evaluation_only():
    assert cli.DEFAULT_CASES_PATH == Path("evals/datasets/smoke.jsonl")


def test_repetitions_are_live_only_and_bounded():
    replay = CliRunner().invoke(
        cli.app,
        ["quality", "--mode", "replay", "--repetitions", "2"],
    )
    unbounded = CliRunner().invoke(
        cli.app,
        ["quality", "--mode", "live", "--repetitions", "11"],
    )

    assert replay.exit_code != 0
    assert "applies only" in replay.output
    assert unbounded.exit_code != 0


def test_human_review_and_release_decision_cli(test_config, tmp_path):
    quality_result = cli.run_quality_replay_evals(test_config, cli.DEFAULT_CASES_PATH)
    report_path = tmp_path / "quality.json"
    form_path = tmp_path / "form.json"
    key_path = tmp_path / "key.json"
    reviews_path = tmp_path / "reviews.json"
    decision_path = tmp_path / "decision.json"
    write_quality_report(quality_result, report_path)
    reviews_path.write_text(
        json.dumps({"rubric_version": "retail_analysis_v1", "reviews": []}),
        encoding="utf-8",
    )

    form = CliRunner().invoke(
        cli.app,
        [
            "human-review-form",
            "--report",
            str(report_path),
            "--form-output",
            str(form_path),
            "--key-output",
            str(key_path),
        ],
    )
    decision = CliRunner().invoke(
        cli.app,
        [
            "release-decision",
            "--report",
            str(report_path),
            "--reviews",
            str(reviews_path),
            "--key",
            str(key_path),
            "--output",
            str(decision_path),
        ],
    )

    assert form.exit_code == 0, form.output
    assert "comparisons=4" in form.output
    assert decision.exit_code == 1
    assert "release=BLOCKED" in decision.output
    assert json.loads(decision_path.read_text(encoding="utf-8"))["approved"] is False
