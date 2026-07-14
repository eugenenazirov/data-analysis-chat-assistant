# Executive Evaluation — 2026-07-14

## Decision

**BLOCKED — not ready for release approval.**

The implementation, deterministic quality gates, BigQuery boundary, and
container separation are healthy. The credentialed model canary is not stable
enough to justify the 34-case, five-repetition release tier, and no independent
human review should be treated as complete while the automated candidate is
failing.

| Evidence | Outcome | Executive interpretation |
|---|---|---|
| 320 tests, 90.84% branch-aware runtime coverage | Pass | Implementation regression gate is healthy |
| Fixture provenance, dataset governance, guardrails | Pass | Evaluation inputs and safety controls are reproducible |
| 67 replay cases across six partitions | Automated pass | Deterministic behavior is healthy; held-out partitions still require live/human proof |
| Runtime/evaluation container separation | Pass | Evaluation code and dependencies do not ship in runtime |
| Current BigQuery smoke under 50 MB cap | Pass | Credentials, guard, dry run, execution, and bounded aggregate work |
| Smoke live canary, 3 repetitions | Fail | Reliability, latency, behavior, and operational budgets are below release thresholds |
| Five-repetition live release candidate | Not run | Canary failure makes the larger run unjustified |
| Two-reviewer blind and pointwise review | Not run | Automated candidate must be stable first |
| Machine release decision | Blocked | Seven explicit blockers are recorded |

## Post-Fix Live Canary

The final canary used `quality-v6`, `analysis-v4`, the current Gemini/Qdrant/
BigQuery path, four smoke cases, three attempts per case, and a 50 MB per-query
BigQuery cap.

| Metric | Result |
|---|---:|
| First-attempt success | 2/4 (50%) |
| Eventual case success | 3/4 (75%) |
| Successful attempts | 5/12 (41.7%) |
| 95% attempt-pass interval | 19.3%–68.0% |
| p50 duration | 31.343 s |
| p95 duration | 106.418 s |
| Flaky cases | 2 |
| Candidate warehouse executions | 14 |
| Duplicate warehouse executions | 0 |
| Candidate dry-run bytes | 106,187,993 |
| Candidate billed bytes | 272,629,760 |
| Canonical snapshots | 4/4 successful |
| Canonical dry-run bytes | 32,032,959 |
| Canonical billed bytes | 52,428,800 |

Every completed analytical comparison scored 1.0 for intent, canonical
calculation, and faithfulness. The overall aggregates are lower because one
attempt ended in a typed model-availability failure before SQL. Retrieval stayed
useful and harmless, but the regional follow-up consistently ranked the primary
precedent second (MRR 0.5), which is acceptable for correctness but worth
monitoring.

## What Was Fixed During Evaluation

1. The evaluation image initially copied root-owned mode-600 fixture data. The
   Docker target now copies evaluation files with non-root ownership; both image
   gates pass.
2. A valid empty query result previously caused a model retry and possible query
   broadening. Empty results are now preserved, counted once, and require an
   explicit no-matching-data narrative without SQL replay.
3. The first live canary exposed a calculation false negative when correct rows
   used unambiguous semantic aliases such as `state`/`region`. Evaluator v6 now
   honors explicit mappings when present and otherwise accepts only one
   compatible alias. Mutation coverage proves the live shape.
4. Repeated output-validation retries exposed weak initial narrative guidance.
   Prompt v4 requires a numeric audit and a concise summary before structured
   output. It eliminated retries for all three customer-spend attempts, but did
   not remove instability in the other cases.
5. Reference SQL is now snapshotted once per case rather than once per repeated
   candidate attempt, with reference cost reported separately.

No threshold was relaxed to improve the result.

## Remaining Release Blockers

- automated live quality did not pass;
- only three of the required five repetitions were run;
- `monthly_revenue_category_critical` and `regional_returns_follow_up` were
  flaky;
- model availability failed repeatedly during the run, including one terminal
  pre-SQL attempt;
- several completed turns exceeded the 30-second per-turn/trajectory budget or
  the allowed query/provider/output-retry budget;
- two independent reviewers have not completed pairwise-first and pointwise
  scoring;
- human dimension floors and accepted-baseline noninferiority are therefore not
  established.

## Recommendation

Do not promote this candidate. First rerun the three-repetition smoke canary in
the protected CI environment with known Vertex quota and collect provider error
codes/latency by turn. The canary must reach 100% first-attempt and per-attempt
success with no flaky case before running the 34-case × 5 release candidate.
Only then distribute the blinded A/B packet, collect two independent reviews,
follow with pointwise scoring, and execute the frozen-artifact approval gate.

## Evidence

- [`live-canary.json`](evaluation/2026-07-14/live-canary.json) — final repeated live report
- [`live-canary-initial.json`](evaluation/2026-07-14/live-canary-initial.json) — pre-fix report retained for comparison
- [`release-decision.json`](evaluation/2026-07-14/release-decision.json) — computed blocked decision
- [`smoke-replay.json`](evaluation/2026-07-14/smoke-replay.json) and
  [`release-holdout-replay.json`](evaluation/2026-07-14/release-holdout-replay.json) — core replay evidence
- remaining replay partitions are in [`evaluation/2026-07-14/`](evaluation/2026-07-14/)
