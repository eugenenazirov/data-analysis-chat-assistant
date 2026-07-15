# Quality Assurance And Release Gates

## Reproducible Environment

Python 3.12 and uv 0.10.8 are pinned. `pyproject.toml` is the dependency source
of truth and `uv.lock` is consumed with `--frozen` locally, in CI, and in Docker.
The runtime uses `pydantic-ai-slim[google]`; `pydantic-evals` belongs only to the
`eval` dependency group.

```bash
just setup
```

## Complete Credential-Free Gate

```bash
just check
```

This one command checks the lockfile and installed packages, runs Ruff, executes
the branch-aware test suite, verifies generated fixture fingerprints and dataset
governance, then runs guardrails and every replay partition.

The credential-free suite covers:

- architecture import direction and absence of evaluation imports in runtime;
- settings precedence, validation, secret masking, and packaged prompts;
- conversation isolation, bounded complete history, trajectory metrics, and
  tool-summary compaction;
- deterministic mandatory retrieval, optional model-selected retrieval/SQL
  paths, valid empty results, bounded retries,
  provider/warehouse failure boundaries, and duplicate-work prevention;
- SQL AST, table/column/join scope, PII, projection, row, cost, timeout, stable
  job-ID, unknown-outcome, and safe-division controls;
- evidence-bound output, expected refusal/clarification/degradation, explicit
  no-data disclosure, recursive redaction, and verified chart binding;
- chart success, all declared plotting dependencies, realistic PNG/SVG/grouped
  bar/line/156-cell heatmap templates, classified repair feedback, retry
  exhaustion, timeout, cleanup, source/output caps, and CLI rendering;
- complete-result metadata, the 500-row client cap, explicit SQL limit meaning,
  deterministic truncated previews, and chart suppression for partial results;
- evaluation contracts, deterministic scoring, reliability statistics, human
  calibration, release decisions, CI definitions, and image separation.

Verified on 2026-07-15: 448 tests passed with 89.56% branch-aware runtime
coverage, above the 85% gate. The dated execution record is maintained in
[`live-test-findings.md`](live-test-findings.md).

## Evaluation Dataset Contract

Fixture generation is deterministic and checked rather than trusted:

```bash
uv run python -m evals.datasets.build_replay_fixtures --check
just dataset
```

Every case declares its suite, category, risk, reference date, expected
behavior, applicable evaluators, canonical SQL, exact result/answer contract,
operational budget, and human rubric. Replay provenance binds the canonical SQL
digest, source tables, schema, row count, capture time, evaluator/prompt/persona/
model/index versions, and a content fingerprint. The loader fails closed on a
stale evaluator version, changed content, duplicates, malformed rows, and
undeclared train/release overlap.

| Partition | Cases | Purpose |
|---|---:|---|
| `smoke.jsonl` | 4 | Fast critical answer, privacy, retrieval, and follow-up sentinels |
| `release_holdout.jsonl` | 30 | Held-out temporal, aggregation, join, zero-row, policy, and limit coverage |
| `multi_turn.jsonl` | 10 | Reference resolution, corrections, comparison context, and trajectory budgets |
| `development.jsonl` | 9 | Unseen-wording retrieval ranking, usefulness, degradation, and harmful precedent |
| `adversarial.jsonl` | 11 | PII, re-identification, prompt injection, destructive SQL, secret, and chart abuse |
| `regression.jsonl` | 3 | Minimized cases for previously observed evaluator/runtime failures |

The 67 cases are all exercised by `just check`; release holdout is separately
validated to have no accidental question or SQL overlap.

## Deterministic Evaluation Gates

### Guardrails

```bash
just guardrails
```

The guardrail suite must pass 100%. It checks read-only aggregate SQL, explicit
safe columns and joins, PII and whole-row blocking, destructive or stacked SQL,
table scope, limits, malformed feedback, recursive output redaction, and
division-by-zero protection. BigQuery calculations use `SAFE_DIVIDE` or an
equivalent explicit zero guard, consistent with the
[BigQuery mathematical functions reference](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/mathematical_functions).

### Answer-quality replay

```bash
just eval
```

The evaluator checks:

- parsed SQL intent, required tables/fragments/functions, equivalent periods,
  and declared join keys;
- canonical result-set calculation accuracy with declared column mappings,
  ordering, units, and numeric tolerance;
- Retrieval Recall@3, MRR, NDCG@3, downstream usefulness, and harmful-example
  influence;
- numeric faithfulness through the runtime evidence policy;
- multi-turn intent resolution and use of prior context;
- expected answer, clarification, refusal, or degraded behavior;
- complete candidate data for analytical answers and a verified artifact for
  every case that requests a chart;
- exact verified SQL attachment, no unsupported row dump, and report integrity;
- provider/tool/query/token/byte budgets, compliant tool ordering, and latency
  observations;
- analyst usefulness when human scores are available.

Quality-v8 makes a three-way decision for SQL intent and result equivalence:

- `PASS` requires structural proof from the parsed SQL, projection lineage, and
  exact complete result values. Equivalent CTE placement, filtered versus
  conditional aggregates, deterministic ranking functions, harmless aliases,
  fixed-year month labels, and helper measures may differ from the canonical
  query when those proofs agree.
- `FAIL` means a declared invariant differs, including source tables, joins,
  filters/literals, time bounds, output grain, aggregate multiplicity, ranking
  semantics, numeric results, completeness, privacy, or required artifacts.
- `REVIEW` is reserved for a value-compatible mapping whose semantic lineage is
  genuinely ambiguous. It blocks the suite but is not included in model failure
  or critical-failure counts. Human usefulness review remains a separate flag
  and message.

This is deliberately conservative: correctness thresholds and hard constraints
are unchanged, and no LLM judge or case-specific alias allowlist can waive them.

Replay is reproducible evidence, not proof of current model or warehouse
behavior. Release cases therefore remain `AUTO PASS` until live and structured
human gates are complete.

## Credentialed Live Evaluation

Prepare the current cached image, Qdrant, Golden Knowledge, diagnostics, and the
actual image-level chart runtime in one command:

```bash
just live-setup
export BQ_MAX_BYTES_BILLED=50000000
```

A local canary can then run three independent attempts per smoke case:

```bash
just release-canary
```

Live mode snapshots the canonical result once per case, not once per repeated
candidate attempt. Candidate-agent and reference-query executions, bytes, job
IDs, and cache rates are accounted separately. Conversation cases merge history
and final-turn telemetry before applying per-trajectory budgets. Reports include
first-attempt, eventual, and per-attempt pass rates, a 95% pass-rate interval,
p50/p95 duration, score variance, worst scores, and explicit flaky cases.
Latency remains visible in the report but is informational for the reviewer
release decision because provider-side response time is outside this prototype's
control. Correctness, completeness, privacy, tool budgets, duplicate warehouse
execution, and required chart artifacts remain blocking gates.

Transient retries are allowed only before verified warehouse work completes.
Successful empty results are retained and require an explicit no-matching-data
answer; they do not trigger broader SQL. Unknown post-submission outcomes remain
non-retryable.

The reviewer default is `google-cloud:gemini-3.5-flash`, authenticated by Vertex
ADC. It uses two bounded global-endpoint transport attempts, then one regional
`us-central1` attempt within the same provider request budget. An explicitly
selected `google:` model instead uses `GOOGLE_API_KEY` and all three attempts on
the Gemini Developer API.

When a completed run identifies a bounded set of failures, rerun only those
checks by repeating `--case-id`:

```bash
uv run python -m evals.run quality \
  --mode live \
  --automated-only \
  --cases evals/datasets/release_holdout.jsonl \
  --case-id first_failed_case \
  --case-id second_failed_case \
  --repetitions 5 \
  --output artifacts/quality-eval-live-failed-rerun.json
```

The command rejects unknown IDs. Keep the dataset, model, reference dates,
prompt, and provider settings identical to the failed run. This subset is
targeted remediation evidence only; it cannot satisfy the complete release or
human-review gate by itself.

## CI Tiers And Immutable Evidence

The workflows separate three decisions:

1. `CI` runs `just check`-equivalent offline verification and container checks
   on pushes and pull requests.
2. `Live answer-quality candidate` runs only on the default branch in the
   protected `quality-live-evaluation` environment with workload identity. The
   daily canary uses smoke × 3; the manually selected release candidate uses
   smoke plus holdout × 5.
3. `Approve live quality candidate` accepts a successful candidate run ID,
   checks the workflow, event, default branch, exact revision, provenance, and
   every recorded SHA-256 digest, then decides from the downloaded artifact and
   submitted reviews without invoking Gemini or BigQuery.

GitHub artifacts are the handoff boundary between runs, matching GitHub's
[workflow artifact guidance](https://docs.github.com/en/actions/concepts/workflows-and-actions/workflow-artifacts).
Canary evidence is retained for 14 days and release/decision evidence for 90
days.

## Human Review And Release Decision

A five-repetition release report produces three restricted materials:

```bash
uv run python -m evals.run human-review-form \
  --report artifacts/quality-eval-live-release.json \
  --cases artifacts/release-cases.jsonl \
  --seed release-specific-seed \
  --pairwise-output artifacts/human-pairwise-form.json \
  --form-output artifacts/human-review-form.json \
  --key-output artifacts/human-review-key.json
```

The review coordinator must distribute only the blinded pairwise packet first.
After the A/B choices are submitted, distribute the pointwise packet. Never
distribute the assignment key to reviewers. Two pseudonymous reviewers score
correctness, faithfulness, usefulness, clarity, limitations, and privacy/policy
on the versioned 1–5 rubric, with notes free of secrets and personal data.

The final decision is computed, not inferred from comments:

```bash
uv run python -m evals.run release-decision \
  --report artifacts/quality-eval-live-release.json \
  --reviews path/to/completed-human-reviews.json \
  --key artifacts/human-review-key.json \
  --output artifacts/release-decision.json
```

Release requires all of the following:

- five live repetitions and no flaky or critical-case failure;
- no truncated analytical result, every requested chart artifact present, no
  duplicate warehouse execution, and all declared operational budgets met;
- complete reviews from at least two reviewers for every required case;
- mean usefulness at least 4/5 and every case at least 3/5;
- every dimension at least 3/5, with correctness, faithfulness, and
  privacy/policy at least 4/5;
- major reviewer disagreements and reject recommendations carry explicit
  resolution notes;
- blinded candidate noninferiority to the accepted baseline of at least 80%;
- no automated, operational, provenance, or structured-review blocker.

`--automated-only` is therefore a regression signal, never release approval.

## Container Separation Gate

```bash
just container-check
```

The production runtime image includes runtime code, configuration, and Golden
Knowledge seed data only. It excludes `evals/`, its datasets, and
`pydantic-evals`. The gate also runs `chart-smoke` in that exact runtime image,
so an allowlisted-but-missing plotting dependency fails before review. The
evaluation image adds the evaluation assets and runs guardrail and replay smoke
tests.

## Manual Resilience Checks

1. Ask a revenue question, follow with “compare that with the prior month” and
   “plot it”; verify one conversation, prior verified context, and a chart.
2. Simulate Gemini failure before SQL; verify a typed retryable failure and that
   the next chat turn remains usable.
3. Simulate model failure after query execution; verify a redacted degraded
   report and no query replay.
4. Return a successful zero-row query; verify one warehouse execution and an
   explicit no-matching-data response.
5. Stop Qdrant; verify typed retrieval degradation while SQL-only analysis can
   still complete when precedent is optional.
6. Attempt retrieval after SQL or chart generation before successful SQL; verify
   the trajectory is rejected.
7. Run chart code that times out, omits output, emits active SVG content, or
   exceeds size limits; verify typed errors and temporary cleanup.
