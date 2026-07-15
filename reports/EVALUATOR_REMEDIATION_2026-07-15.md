# Evaluator Remediation And Reviewer Readiness

Date: 2026-07-15

## Outcome

The prototype is ready for the company reviewer walkthrough. The runtime,
warehouse, privacy, completeness, retrieval, multi-turn, and chart paths worked
through the full live execution. The apparent release failures came from an
overly literal quality-v7 evaluator rejecting correct Gemini SQL variants.

Quality-v8 now accepts only equivalence proved by parsed SQL structure,
projection lineage, and exact complete result values. It retains hard failures
for different tables, joins, filters, periods, output grain, calculations,
ranking/tie semantics, incomplete results, privacy violations, and missing chart
artifacts. If equivalence cannot be proved or disproved, it emits `REVIEW` and
blocks the suite without counting the outcome as a Gemini failure.

Formal release approval remains pending the existing blinded two-reviewer gate.
That is a governance step, not an application reliability defect.

## Evidence Sequence

The historical reports remain unchanged. This report records the remediation
that followed them.

1. The 34-case suite ran five times with Gemini 3.5 Flash on Vertex AI global,
   prompt `analysis-v11`, fixed reference dates, and the same BigQuery data.
2. All 170 attempts completed. There were zero runtime/provider terminal
   failures and zero duplicate warehouse executions. Every one of the 10 chart
   attempts produced a non-empty verified artifact.
3. Quality-v7 marked 14 cases failed or ambiguous because it compared outer SQL
   syntax too literally across CTEs, window functions, conditional aggregates,
   aliases, and fixed-year month labels.
4. Quality-v8 was implemented with structural and lineage proofs plus exact
   complete-result disambiguation. The hard semantic and operational
   constraints were not lowered.
5. The stored 170 attempts were regraded deterministically: 170/170 had passing
   intent and calculation assessments, with zero ambiguous semantic reviews.
6. Only the 14 affected checks were rerun live, as required by the remediation
   protocol: 70/70 fresh attempts passed.

## Targeted Live Rerun

| Evidence | Smoke subset | Holdout subset | Combined |
|---|---:|---:|---:|
| Cases | 1 | 13 | 14 |
| Attempts | 5 | 65 | 70 |
| Successful attempts | 5 | 65 | 70 |
| First-attempt success | 100% | 100% | 100% |
| Per-attempt success | 100% | 100% | 100% |
| Flaky cases | 0 | 0 | 0 |
| Critical failures | 0 | 0 | 0 |
| Semantic reviews | 0 | 0 | 0 |
| Duplicate warehouse executions | 0 | 0 | 0 |
| p95 duration | 9.15 s | 12.06 s | informational |

The holdout subset needed six bounded SQL-draft repairs, so first-draft SQL
validity was 90.77%. All repairs completed within their agent turns; every case
passed on its first full attempt, and exactly 65 BigQuery executions served 65
successful holdout attempts. Provider retries did not repeat completed warehouse
work.

The affected cases were:

- `regional_returns_follow_up`
- `quarter_over_quarter_state_growth`
- `fixed_cohort_period_comparison`
- `net_revenue_after_returns`
- `unit_return_rate_by_category`
- `returned_revenue_rate_by_category`
- `minimum_sample_return_rate`
- `top_three_products_per_category`
- `state_share_of_realized_revenue`
- `equal_revenue_tie_handling`
- `deterministic_empty_product_cohort`
- `single_vs_repeat_customer_spend`
- `new_vs_repeat_revenue_mix`
- `distinct_orders_with_multi_item_rows`

Machine-readable local artifacts:

- `artifacts/quality-eval-live-release.json`: original complete 170-attempt run
- `artifacts/quality-eval-live-failed-rerun-smoke.json`: five fresh smoke attempts
- `artifacts/quality-eval-live-failed-rerun-holdout.json`: 65 fresh holdout attempts

These artifacts are intentionally ignored local evidence and may contain live
business aggregates. The committed report contains no credential, project ID,
account name, token, or machine-local credential path.

## Evaluator Changes

- SQL is parsed and normalized into semantic signatures rather than compared as
  text.
- Projection evidence follows aliases and lineage through CTEs.
- Output grain is derived from projected lineage, not only the outer `GROUP BY`.
- Filtered and conditional aggregates are compared by business predicates and
  aggregate multiplicity.
- Ranking functions are equivalent only when secondary order keys provably
  complete the grouped grain; explicit tie-preserving semantics remain distinct.
- Aggregate aliases in `HAVING`, safe-division formulas, helper measures, and
  fixed-year SQL-proven month labels are supported.
- Exact complete result values may prove one unique alias mapping. Duplicate or
  otherwise ambiguous mappings remain `REVIEW`.
- Repeatable `--case-id` selection supports targeted remediation and rejects
  unknown case IDs.

## Reliability Cleanup

The final simplify/review pass also corrected provider failure classification.
Only 408, 429, retryable 5xx responses, connection failures, and timeouts can
trigger the Vertex regional fallback. Non-retryable 400/401/403 responses now
fail once with normalized telemetry and a zero retry count. A 120-second
per-provider-request timeout prevents silent multi-minute hangs.

## Verification

- `just check`: pass; 448 tests, 89.56% branch-aware runtime coverage, all
  fixtures, dataset governance, guardrails, and 67 replay cases pass.
- Targeted provider/evaluator/CLI tests: 164 pass.
- Full saved-attempt semantic regrade: 170/170 pass under quality-v8.
- Targeted live rerun: 70/70 pass.
- Image diagnostics at the live checkpoint: revision and prompt stamps matched;
  model `google-cloud:gemini-3.5-flash`; prompt `analysis-v11`; evaluator
  `quality-v8`.
- Image chart smoke: PNG, SVG, pandas line, seaborn grouped bar, and 156-cell
  heatmap all pass.

The remaining readiness checks are the final current-image container/reviewer
gate, clean manual walkthrough, visual artifact inspection, and the independent
human-review packet for formal release approval.
