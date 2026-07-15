# Verification Record

The current dated evidence is:

- [Evaluator remediation and reviewer-readiness report](../reports/EVALUATOR_REMEDIATION_2026-07-15.md)
- [Reviewer reliability implementation record](../reports/REVIEWER_RELIABILITY_2026-07-15.md)
- [Executive evaluation](../reports/EXECUTIVE_EVALUATION_2026-07-14.md)
- [Testing report](../reports/TESTING_REPORT_2026-07-14.md)
- [Machine-readable evaluation evidence](../reports/evaluation/2026-07-14/)

## Current Result

Verified on 2026-07-15 with Python 3.12 and uv 0.10.8:

| Gate | Result |
|---|---|
| Lockfile, environment, and Ruff | Pass |
| Pytest | 448 passed |
| Branch-aware runtime coverage | 89.64% (85% gate) |
| Fixture provenance and dataset governance | Pass |
| Replay evaluation | 67/67 automated pass across six partitions |
| BigQuery smoke under 50 MB cap | Pass; 4,356,216 dry-run bytes, one row |
| Runtime/evaluation image boundary | Pass |
| Five-repetition full live execution | 170 attempts completed; zero runtime failures, zero duplicate warehouse executions, 10/10 chart artifacts present |
| Saved-attempt quality-v8 semantic regrade | 170/170 pass; zero ambiguous reviews |
| Targeted live remediation rerun | 70/70 pass across all 14 formerly affected cases; zero flaky/critical cases or semantic reviews |
| Independent analyst review | Pending |
| Company reviewer walkthrough | **Ready** |
| Formal release decision | **Pending human review** |

## Interpretation

The application/runtime path is healthy. The original 34-case, five-repetition
run completed without a provider/runtime failure, completeness failure, missing
chart, privacy failure, or duplicate warehouse execution. Its quality-v7
evaluator rejected correct but differently structured Gemini SQL. Quality-v8
keeps the same hard invariants and accepts only equivalence proved through SQL
structure, projection lineage, and exact complete results; unresolved mappings
remain release-blocking `REVIEW` outcomes.

All 170 saved attempts passed a deterministic quality-v8 semantic regrade. Per
the remediation protocol, only the 14 affected cases were then run live again:
70/70 fresh attempts passed, with a 12.06-second p95 on the larger subset. The
complete automated evidence is sufficient for a company reviewer walkthrough.
It is not represented as formal release approval because the required blinded
two-reviewer packet has not been completed, and the targeted rerun intentionally
is not a replacement for a new immutable full-suite artifact.

`--automated-only` is a regression signal, never release approval. The computed
decision also requires five clean live repetitions, no flaky case, two
independent reviewers, human dimension floors, and blinded baseline
noninferiority. See the executive report for the exact blockers and next action.

No machine-specific account name, project ID, token, credential, or local
credential path is committed in these narrative reports.
