from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from evals.dataset import (
    inspect_dataset_governance,
    validate_partition_path,
    validate_quality_case_schema,
)
from evals.guardrails import run_guardrail_evals
from evals.human import (
    evaluate_release_readiness,
    load_accepted_baseline,
    load_human_review_key,
    load_human_review_set,
    write_human_review_packet,
    write_release_decision,
)
from evals.quality import (
    QualitySuiteResult,
    load_human_scores,
    load_quality_cases,
    run_quality_live_evals,
    run_quality_replay_evals,
    write_quality_report,
)
from retail_agent.bootstrap import Runtime
from retail_agent.infrastructure.settings import load_settings

app = typer.Typer(help="Retail assistant evaluation runner")
console = Console()
DEFAULT_CASES_PATH = Path("evals/datasets/smoke.jsonl")
DEFAULT_SCHEMA_PATH = Path("evals/datasets/schema/quality-case.schema.json")
DEFAULT_BASELINE_PATH = Path("evals/baselines/accepted-smoke-v1.json")


class QualityEvalMode(StrEnum):
    replay = "replay"
    live = "live"


@app.command("validate-dataset")
def validate_dataset(
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
    cases_path: Annotated[
        Path, typer.Option("--cases", help="Evaluation JSONL dataset to validate.")
    ] = DEFAULT_CASES_PATH,
    schema_path: Annotated[
        Path, typer.Option("--schema", help="Committed evaluation case JSON Schema.")
    ] = DEFAULT_SCHEMA_PATH,
) -> None:
    """Validate schema, partition ownership, hashes, uniqueness, and overlap policy."""

    config = load_settings(config_path)
    cases = load_quality_cases(cases_path)
    validate_partition_path(cases_path, cases)
    validate_quality_case_schema(schema_path)
    governance = inspect_dataset_governance(cases, config.golden_trios_path)
    console.print(
        "[green]dataset_valid[/green] "
        f"cases={len(cases)} "
        f"question_overlap={len(governance.golden_question_overlap_ids)} "
        f"sql_overlap={len(governance.golden_sql_overlap_ids)} "
        f"intentional_overlap={governance.intentional_overlap_count}"
    )


@app.command()
def guardrails(
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
) -> None:
    """Run deterministic SQL and privacy guardrail evaluations."""

    results = run_guardrail_evals(load_settings(config_path))
    table = Table(title="Guardrail evals")
    table.add_column("Eval")
    table.add_column("Status")
    table.add_column("Detail")
    for result in results:
        table.add_row(result.name, "PASS" if result.passed else "FAIL", result.detail)
    console.print(table)
    if not all(result.passed for result in results):
        raise typer.Exit(code=1)


@app.command()
def quality(
    config_path: Annotated[str, typer.Option("--config")] = "config/agent.yaml",
    mode: Annotated[
        QualityEvalMode, typer.Option(help="Quality suite execution mode.")
    ] = QualityEvalMode.replay,
    cases_path: Annotated[
        Path, typer.Option("--cases", help="Quality evaluation JSONL dataset.")
    ] = DEFAULT_CASES_PATH,
    case_ids: Annotated[
        list[str],
        typer.Option(
            "--case-id",
            help="Run only this case ID; repeat the option to select multiple cases.",
        ),
    ] = [],
    output: Annotated[
        Path | None, typer.Option(help="Optional machine-readable quality report.")
    ] = None,
    human_scores: Annotated[
        Path | None,
        typer.Option(help="JSON mapping case IDs to analyst usefulness scores (0-5)."),
    ] = None,
    automated_only: Annotated[
        bool,
        typer.Option(help="Gate automated metrics without requiring analyst scores."),
    ] = False,
    repetitions: Annotated[
        int,
        typer.Option(
            min=1,
            max=10,
            help="Independent live attempts per case (replay remains single-pass).",
        ),
    ] = 1,
    inter_attempt_delay: Annotated[
        float,
        typer.Option(
            min=0,
            help=(
                "Seconds between live attempts and multi-turn requests to smooth provider traffic."
            ),
        ),
    ] = 5.0,
) -> None:
    """Run replay or credentialed live answer-quality evaluations."""

    config = load_settings(config_path)
    if mode is QualityEvalMode.replay:
        if repetitions != 1:
            raise typer.BadParameter("--repetitions applies only to --mode live")
        result = run_quality_replay_evals(
            config,
            cases_path,
            case_ids=set(case_ids) or None,
        )
    else:
        runtime = Runtime(config)
        result = asyncio.run(
            run_quality_live_evals(
                config,
                cases_path,
                bigquery=runtime.bigquery,
                golden_store=runtime.golden_store,
                logger=runtime.logger,
                analysis_agent=runtime.analysis_agent,
                analysis_model=runtime.analysis_model,
                chart_executor=runtime.chart_executor,
                human_scores=load_human_scores(human_scores),
                repetitions=repetitions,
                inter_attempt_delay_seconds=inter_attempt_delay,
                case_ids=set(case_ids) or None,
            )
        )
        output = output or Path("artifacts/quality-eval-live.json")

    table = Table(title=f"Answer-quality evals ({result.mode})")
    table.add_column("Eval")
    table.add_column("Status")
    table.add_column("Scores")
    for case_result in result.results:
        if case_result.passed:
            status = "PASS"
        elif case_result.semantic_review_required:
            status = "REVIEW"
        elif case_result.automated_passed:
            status = "AUTO PASS"
        else:
            status = "FAIL"
        table.add_row(
            (
                f"{case_result.name}#{case_result.attempt}"
                if result.repetitions > 1
                else case_result.name
            ),
            status,
            case_result.detail,
        )
    console.print(table)
    if result.needs_human_review:
        console.print("[yellow]Human usefulness scores are required before release.[/yellow]")
    if result.semantic_review_required:
        console.print(
            "[yellow]Semantic equivalence is indeterminate and requires review before release."
            "[/yellow]"
        )
    if output is not None:
        write_quality_report(result, output)
        console.print(f"[dim]quality_report={output}[/dim]")
    gate_passed = result.automated_passed if automated_only else result.passed
    if not gate_passed:
        raise typer.Exit(code=1)


@app.command("human-review-form")
def human_review_form(
    report_path: Annotated[
        Path, typer.Option("--report", help="Machine-readable quality report to review.")
    ],
    cases_path: Annotated[
        Path, typer.Option("--cases", help="Dataset used to produce the report.")
    ] = DEFAULT_CASES_PATH,
    baseline_path: Annotated[
        Path, typer.Option("--baseline", help="Last accepted comparison baseline.")
    ] = DEFAULT_BASELINE_PATH,
    form_output: Annotated[
        Path, typer.Option("--form-output", help="Reviewer-facing scoring packet.")
    ] = Path("artifacts/human-review-form.json"),
    pairwise_output: Annotated[
        Path,
        typer.Option("--pairwise-output", help="Blinded A/B packet distributed first."),
    ] = Path("artifacts/human-pairwise-form.json"),
    key_output: Annotated[
        Path,
        typer.Option(
            "--key-output",
            help="Restricted A/B assignment key; do not share with reviewers.",
        ),
    ] = Path("artifacts/human-review-key.json"),
    seed: Annotated[
        str, typer.Option(help="Release-specific seed used to balance A/B positions.")
    ] = "local-review",
) -> None:
    """Create absolute and blinded pairwise analyst-scoring materials."""

    result = QualitySuiteResult.model_validate_json(report_path.read_text(encoding="utf-8"))
    cases = load_quality_cases(cases_path)
    form, pairwise, key = write_human_review_packet(
        cases,
        result,
        load_accepted_baseline(baseline_path),
        form_path=form_output,
        pairwise_path=pairwise_output,
        key_path=key_output,
        seed=seed,
    )
    console.print(
        "[green]human_review_form_created[/green] "
        f"cases={len(form.cases)} comparisons={len(pairwise.cases)} "
        f"form={form_output} pairwise={pairwise_output} key={key_output}"
    )


@app.command("release-decision")
def release_decision(
    report_path: Annotated[
        Path, typer.Option("--report", help="Machine-readable credentialed quality report.")
    ],
    reviews_path: Annotated[
        Path, typer.Option("--reviews", help="Completed structured human review set.")
    ],
    key_path: Annotated[Path, typer.Option("--key", help="Restricted A/B assignment key.")],
    output: Annotated[Path, typer.Option(help="Machine-readable release decision.")] = Path(
        "artifacts/release-decision.json"
    ),
) -> None:
    """Combine automated, repeated-live, human, and baseline evidence."""

    quality_result = QualitySuiteResult.model_validate_json(report_path.read_text(encoding="utf-8"))
    reviews = load_human_review_set(reviews_path)
    key = load_human_review_key(key_path)
    decision = evaluate_release_readiness(
        quality_result,
        reviews,
        key.assignments,
        baseline_version=key.baseline_version,
    )
    write_release_decision(decision, output)
    status = "APPROVED" if decision.approved else "BLOCKED"
    console.print(
        f"release={status} reviewers={decision.human_review.reviewer_count} "
        f"reviewed_cases={decision.human_review.reviewed_case_count} output={output}"
    )
    if decision.blockers:
        console.print("blockers=" + "; ".join(decision.blockers))
    if not decision.approved:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
