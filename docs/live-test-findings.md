# Verification Record

The current dated evidence is:

- [Executive evaluation](../reports/EXECUTIVE_EVALUATION_2026-07-14.md)
- [Testing report](../reports/TESTING_REPORT_2026-07-14.md)
- [Machine-readable evaluation evidence](../reports/evaluation/2026-07-14/)

## Current Result

Verified on 2026-07-14 with Python 3.12 and uv 0.10.8:

| Gate | Result |
|---|---|
| Lockfile, environment, and Ruff | Pass |
| Pytest | 320 passed |
| Branch-aware runtime coverage | 90.84% (85% gate) |
| Fixture provenance and dataset governance | Pass |
| Replay evaluation | 67/67 automated pass across six partitions |
| BigQuery smoke under 50 MB cap | Pass; 4,356,216 dry-run bytes, one row |
| Runtime/evaluation image boundary | Pass |
| Three-repetition live canary | Fail; 5/12 attempts passed, two flaky cases |
| Five-repetition release candidate | Not run after failed canary |
| Independent analyst review | Pending |
| Release decision | **Blocked** |

## Interpretation

The deterministic application/evaluation gates are healthy, and the live run
preserved safe boundaries: canonical queries succeeded, completed analytical
comparisons were correct and faithful, and duplicate warehouse executions stayed
at zero. The live candidate still failed reliability and latency budgets because
of provider availability, SQL/output retry variance, and degraded responses.

`--automated-only` is a regression signal, never release approval. The computed
decision also requires five clean live repetitions, no flaky case, two
independent reviewers, human dimension floors, and blinded baseline
noninferiority. See the executive report for the exact blockers and next action.

No machine-specific account name, project ID, token, credential, or local
credential path is committed in these narrative reports.
