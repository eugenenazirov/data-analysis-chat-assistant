from pathlib import Path

from typer.testing import CliRunner

from evals import run as cli
from evals.quality import (
    evaluate_quality_case,
    load_quality_cases,
    summarize_quality_results,
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


def test_quality_automated_only_accepts_pending_human_review(
    test_config, monkeypatch
):
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
