# Reviewer-Reliability Evidence — 2026-07-15

## Decision

**Implementation complete; live release gate blocked.**

The prototype's supported reviewer path is now reproducible, diagnosable, and capable of producing real charts in the application container. Offline verification, production-image verification, the reviewer walkthrough, PII behavior, and chart generation all pass. The final Gemini 2.5 Flash canary does not meet the project's repeated-live reliability and latency thresholds, so this report does not call the project release-ready.

The 34-case, five-repetition release run and blinded human-review packet were intentionally not started after the prerequisite canary failed. This preserves the release-gate semantics rather than spending more warehouse/model budget on a candidate that cannot be promoted.

## Scope and production boundary

- `LocalPythonChartExecutor` remains in the application container for this test-assignment prototype.
- The existing subprocess limits, AST policy, timeout, process-group cleanup, output caps, and artifact validation remain in place.
- A production deployment is still expected to replace the local adapter through `ChartCodeExecutor` with an isolated execution service. No external runner or additional sandbox-hardening work is claimed here.

## Implemented reliability changes

- Added the explicit chart runtime: Matplotlib, NumPy, pandas, and seaborn. The image build imports every package and stamps its version.
- Added prompt `analysis-v5` with exact PNG, SVG, grouped-bar, line, and 156-cell month/category heatmap contracts. PNG is the default; SVG is explicit-only.
- Added bounded stderr capture, classified chart failures, repair hints with category/line/filename/columns, two `ModelRetry` repairs, and tool hiding after three total attempts.
- Added recursive PII redaction before `input.json` is written.
- Added the five-case `chart-smoke` image-level test and artifact validation.
- Removed implicit SQL `LIMIT 100`; BigQuery retrieval is capped at 500 while preserving the complete `RowIterator.total_rows` count.
- Added `available_rows`, `truncated`, and `row_limit` across query/report/history/telemetry/evaluation data. Results above 500 are not charted or described as complete and receive a deterministic 20-row preview.
- Added one reusable Google provider/model instance, three retryable transport attempts, global LLM routing, regional embeddings, and normalized provider-failure telemetry.
- Hid all data tools for schema-only questions, hid SQL after one successful execution, bounded narrative output, and made Golden Knowledge degradation explicit.
- Added cached-image reviewer commands: `live-setup`, `diagnostics`, `chart-smoke`, `reviewer-live`, `release-canary`, and `release-live`. `ask`, `chat`, and `index-golden` build the current image first.
- Updated the single root reviewer walkthrough with commands, expected tool sequences, artifacts, and failure interpretation.

## Deterministic verification

| Gate | Result | Evidence |
|---|---:|---|
| `just check` | PASS | Ruff clean; 351 tests passed; 89.58% coverage; all dataset, guardrail, replay, holdout, multi-turn, retrieval, adversarial, and regression checks passed. |
| `just container-check` | PASS | Runtime/evaluation image separation passed; image-level guardrails and replay quality passed. |
| `just review` | PASS | Clean combined setup, local checks, image builds, and container checks completed with exit code 0. |
| `just diagnostics` | PASS | Image and worktree both `6d24a21`; prompt `analysis-v5`; LLM location `global`; embedding location `us-central1`; row cap 500; provider attempts 3. |
| Image chart runtime | PASS | Matplotlib 3.11.0, NumPy 2.5.1, pandas 2.3.3, seaborn 0.13.2 imported during build. |
| `chart-smoke` | PASS | 5/5: PNG 16,564 B; SVG 15,962 B; pandas line 38,565 B; seaborn bar 26,254 B; 156-cell heatmap 143,562 B. |
| Reviewer live walkthrough | PASS | Schema, analytical SQL, PII refusal, chart, multi-turn, and retrieval-degradation flows completed; chart and multi-turn reruns produced real artifacts. |

The production-image diagnostics were run after the final code changes. The direct `docker build` acceptance image uses an `unknown` revision when no build argument is supplied; every reviewer-facing Compose recipe supplies and verifies the actual Git revision.

## Visual artifact inspection

- Live model PNG: `reports/evaluation/2026-07-15/live-model-chart.png` — opened at 1582×936; ten bars are visible, ordered, labeled, and not clipped.
- Smoke SVG: `reports/evaluation/2026-07-15/chart-smoke-matplotlib.svg` — XML and SVG validation passed; rendered via Quick Look; all six month labels and the line are visible.
- The final 2.5 canary produced a verified artifact on every chart-request attempt.

## Model bakeoff

Both candidates used the same four smoke cases, three repetitions, data, prompt version, reference date, provider settings, and evaluation code.

### Gemini 3.5 Flash

- Result: **0/12 attempts passed**.
- All attempts terminated before retrieval or BigQuery; 36 provider requests were made, matching three transport attempts per evaluation attempt.
- The Vertex endpoint available to this project returned HTTP 404 for the model, so it cannot be a reviewer default in this environment even though `gemini-3.5-flash` is a documented stable Gemini model.
- Evidence: `reports/evaluation/2026-07-15/gemini-3.5-flash-canary.json`.

### Gemini 2.5 Flash

- Result: **6/12 attempts passed; 3/4 cases passed at least once**.
- No duplicate warehouse execution occurred.
- All three chart attempts produced verified artifacts.
- PII-safe customer-spend passed 3/3; product-return passed 2/3.
- p50 was 30,961 ms; p95 was 58,331.65 ms, above the documented 20-second target and relevant per-case budgets.
- Flaky cases: `monthly_revenue_category_critical`, `product_return_risk`.
- Critical failure: `monthly_revenue_category_critical`.
- Regional follow-up passed semantic and multi-turn checks once, had one history-turn failure, and produced one semantically incorrect trajectory.
- Evidence: `reports/evaluation/2026-07-15/gemini-2.5-flash-canary.json` and the focused automated chart pass `reports/evaluation/2026-07-15/chart-live-probe.json` (the file-level `passed` flag remains false because human usefulness review was intentionally not supplied).

Gemini 2.5 Flash remains the default because 3.5 is unavailable on the configured Vertex project. It is **not promoted as release-ready**: the winning available model still fails the hard reliability and latency gates.

## Remaining blockers

1. Reduce end-to-end and per-turn latency without increasing the documented budgets. The chart case reached 39.7–54.4 seconds on two attempts; product return reached 33.5 seconds; regional multi-turn reached 63.1 seconds once.
2. Eliminate remaining output-evidence retries without weakening numeric faithfulness. One product-return attempt exhausted three `unsupported_numeric_claim` repairs.
3. Stabilize regional multi-turn query intent. One attempt lost required `created_at`/`DATE_TRUNC` semantics and one history turn failed.
4. Re-run `just release-canary MODEL=google-cloud:gemini-2.5-flash`. Only after 100% first-attempt and per-attempt success should `just release-live` and the blinded two-reviewer packet run.

## Reviewer commands

```bash
just live-setup
just reviewer-live
just release-canary MODEL=google-cloud:gemini-2.5-flash
```

The exact expected flow and failure interpretation are maintained in `PROJECT_WALKTHROUGH.md`. Historical reports under `reports/` were not modified.
