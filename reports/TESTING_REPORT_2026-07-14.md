# Testing Report — 2026-07-14

## Scope

This report covers the sequential evaluation rollout from base `f6e0eda`
through corrective commit `0cf7601`, including the required specialist cleanup,
documentation audit, current cloud smoke, two repeated live canaries, and the
computed release decision.

## Final Credential-Free Matrix

| Gate | Command | Result |
|---|---|---|
| Lockfile and environment | `uv lock --check`, `uv pip check` | Pass |
| Lint | `uv run ruff check .` | Pass |
| Tests | `uv run pytest --cov=retail_agent` | 320 passed |
| Runtime branch coverage | same | 90.84% (85% minimum) |
| Fixture regeneration check | `python -m evals.datasets.build_replay_fixtures --check` | Pass |
| Dataset validation | `just dataset` | 67 valid cases; release holdout has zero accidental overlap |
| SQL/privacy guardrails | `just guardrails` | 100% pass |
| Smoke replay | quality replay | 4/4 pass |
| Release holdout replay | automated replay | 30/30 pass |
| Multi-turn replay | automated replay | 10/10 pass |
| Retrieval replay | automated replay | 9/9 pass |
| Adversarial replay | automated replay | 11/11 pass |
| Regression replay | automated replay | 3/3 pass |
| Runtime image boundary | `just container-check` | Pass; no `evals/` or `pydantic-evals` |
| Evaluation image | `just container-check` | Pass as non-root; guardrails and replay pass |

## Current Cloud Checks

BigQuery smoke passed against the public retail dataset with one bounded
aggregate row, 4,356,216 dry-run bytes, and `BQ_MAX_BYTES_BILLED=50,000,000`.
No cloud project ID, credential, access token, local account, or credential path
is stored in this report.

The first three-repetition live canary failed and led to a targeted evaluator and
prompt fix. The same canary was rerun afterward. Comparison:

| Metric | Initial (`quality-v5` / `analysis-v3`) | Final (`quality-v6` / `analysis-v4`) |
|---|---:|---:|
| First-attempt success | 25% | 50% |
| Eventual success | 75% | 75% |
| Per-attempt success | 33.3% | 41.7% |
| p50 duration | 32.788 s | 31.343 s |
| p95 duration | 76.048 s | 106.418 s |
| Flaky cases | 3 | 2 |
| Calculation aggregate | 0.75 | 0.9167 |
| Operational aggregate | 0.4167 | 0.5 |

The calculation aggregate is not 1.0 only because the final report assigns zero
scores to the one terminal provider-failure attempt. All completed calculation
comparisons passed after the alias fix.

## Regression Probes Added

- stale evaluator versions and changed fixture fingerprints fail closed;
- a valid zero-row query is not retried and must be disclosed explicitly;
- warehouse executions are counted independently of optional job IDs;
- retrieval after SQL is rejected across the full trajectory;
- multi-turn history/final operational telemetry is merged and budgeted;
- canonical reference SQL executes once per case across repeated attempts;
- compatible result aliases pass only when unambiguous;
- blind A/B output is separated from pointwise candidate scoring;
- dimension floors, reject handling, default-branch provenance, and protected
  live environments block release when incomplete.

## Specialist Cleanup Result

Five independent reviews examined reuse, simplification, efficiency, abstraction
level, and project compliance. Their actionable findings were implemented before
the final docs/evaluation work: single-source rubric/prompt versions, blind
packet separation, correct tool-order scanning, valid empty-result preservation,
reference-query de-duplication, trajectory metrics, critical human dimension
floors, protected live execution, and candidate branch/event validation.

## Commit Trail

1. `bcf7793` — validated evaluation contract
2. `196fdc0` — partitioned and fingerprinted data
3. `7cd20b5` — deterministic diagnostics
4. `1129c61` — held-out and conversation coverage
5. `d6520f4` — retrieval and adversarial coverage
6. `4281abf` — reliability and repeated-run reporting
7. `8ab3e57` — calibrated human release gates
8. `7f1fe13` — tiered quality workflows
9. `66b7b8f` — specialist cleanup fixes
10. `4cfcb58` — documentation reconciliation
11. `0cf7601` — live-discovered evaluator/prompt fix

## Conclusion

The code and deterministic evaluation system pass. The current live model
candidate does not. The release decision is correctly blocked, with machine
evidence retained under [`evaluation/2026-07-14/`](evaluation/2026-07-14/).
