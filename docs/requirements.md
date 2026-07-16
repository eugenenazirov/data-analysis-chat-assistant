# Requirements Mapping

The CLI prototype implements the highest-risk analysis path. The production HLD
in `docs/architecture.md` adds durable identity, storage, authorization, and
operations without presenting those components as already built.

## 1. Hybrid Intelligence

Implemented:

- Approved Question -> SQL -> Analyst Report trios are embedded into Qdrant.
- `retrieve_golden_examples` is a bounded agent tool. The model chooses it when
  approved metric, cohort, join, filter, ranking, time-window, comparison,
  return, customer-behavior, or follow-up precedent would improve the analysis,
  and skips it when it would not add value.
- Retrieval receives bounded multi-turn context and reports matched IDs and
  degraded dependency state through typed results and telemetry.
- A Qdrant outage does not prevent a SQL-backed answer.

Production extension:

- Interactions enter an analyst-review queue rather than the active index.
- PostgreSQL approval and an outbox event commit atomically.
- Workers store versioned source artifacts, build a candidate Qdrant collection,
  run release gates, and promote an alias with rollback to the prior version.

## 2. Safety And PII Masking

Implemented controls are layered:

- `sqlglot` accepts one read-only query against fully qualified allowlisted
  tables.
- Table-specific safe-column allowlists reject PII, `SELECT *`, whole-row alias
  projection, DDL, and DML.
- Row limits, BigQuery dry-run byte caps, execution timeouts, and stable job IDs
  bound cost and avoid unsafe resubmission.
- Successful data output requires verified query rows; numeric claims and chart
  references are checked before recursive PII redaction.
- Tool summaries and telemetry are redacted and bounded.

Production adds OIDC identity, least-privilege workload credentials, row-level
security for owned data, restricted encrypted transcripts, network policies,
secret injection, and immutable audit exports.

## 3. Destructive Saved Reports Oversight

This remains production-design-only because the CLI has no Saved Reports
library.

- Preview is scoped to the authenticated owner.
- Candidate report IDs are frozen and hashed; the count and sample titles are
  displayed.
- Confirmation is one-time, owner-bound, idempotent, and expires after five
  minutes.
- A transaction locks the pending action, deletes exactly the frozen owned IDs,
  consumes the token, and appends the audit event.
- Reports created after preview are never silently included.
- Audit exports use object lock with 400-day retention.

## 4. Continuous Improvement

Implemented:

- Formatting preferences and persona content are versioned configuration and
  packaged prompt resources.
- Evaluation code and datasets are isolated from the runtime package and image.
- Guardrail and answer-quality suites exercise the same evidence policy as the
  runtime.

Production:

- User preferences are versioned in PostgreSQL through an authenticated API.
- Only analyst-approved interactions enter Golden Knowledge.
- Candidate indexes must pass retrieval and answer-quality gates before
  promotion.
- Prompt, persona, model, and index versions are recorded per turn for comparison
  and rollback.

## 5. Conversational Analysis And Automatic Charts

Implemented:

- A conversation aggregate retains complete, bounded prior turns rather than a
  single previous-question string.
- PydanticAI receives compacted verified tool context and uses configured
  request, tool-call, token, SQL-retry, chart-retry, and output-retry budgets.
- Retrieval and SQL remain model-selected and retrieval degradation does not
  block SQL. Schema-only questions use structured model output with all data
  tools hidden; `generate_chart` is dynamically unavailable until SQL succeeds
  in the current run.
- Chart code receives only verified rows through a fixed input file and must
  produce a validated PNG or passive SVG through a fixed output path.
- Chart input is recursively redacted before execution. The runtime declares
  and build-verifies Matplotlib, NumPy, pandas, and seaborn; failures are
  classified with bounded line/column/filename repair feedback and receive at
  most two tool repairs (three total attempts).
- The local subprocess has source, output, captured-output, and time limits, a
  minimal environment, process-group cleanup, and digest-only telemetry.

The local subprocess is a reliability boundary, not a security sandbox.
Production must move generated code to an isolated worker with separate
credentials, filesystem, network, CPU, memory, and time controls.

## 6. Resilience And Graceful Error Handling

Implemented:

- SQL validation, pre-submission dry-run, and cost failures use a configurable
  0-3 tool retry budget. A successfully executed empty result is not a failure:
  it is preserved, counted once, and must be disclosed explicitly without
  broadening or replaying the query.
- BigQuery results use a 500-row client retrieval cap rather than an injected
  SQL limit. Returned and available counts are carried separately. Partial
  results produce a deterministic 20-row preview and cannot be charted or
  described as complete; explicit literal limits up to 500 remain valid.
- Every submitted query receives a stable trace/SQL-derived BigQuery job ID.
  An unknown post-submission outcome is non-retryable and never returned to the
  model retry loop.
- Qdrant failure degrades retrieval without failing the turn.
- One reusable Gemini model uses three transport attempts with exponential
  backoff and jitter for 408, 429, and retryable 5xx responses. With Vertex
  credentials, two attempts use the global endpoint and the final attempt uses
  `us-central1` inside the same logical model request; completed tools are not
  restarted.
  Gemini 3.5 Flash runs at temperature 0 with thinking disabled and a bounded
  output budget for predictable reviewer latency; the model remains configurable.
  Provider status, retry count, and terminal category are normalized without
  logging response bodies. Provider failures are translated at the agent boundary. If verified rows
  exist, the user receives a redacted degraded table and executed SQL; otherwise
  the user receives a typed safe failure.
- `chat` survives a failed turn; `ask` exits nonzero without a traceback.
- Chart failure is typed and cannot invalidate the verified analytical result.

Production adds circuit breaking, durable idempotency, HA PostgreSQL, bounded
Kubernetes concurrency, backups, and tested recovery objectives.

## 7. Quality Assurance

- Pytest covers architecture boundaries, configuration, conversations,
  PydanticAI tool selection and structured output, SQL safety, dependency
  degradation, chart execution, CLI rendering, and evaluator behavior.
- Branch-aware runtime coverage is gated at 85%.
- `python -m evals.run guardrails` runs deterministic safety cases.
- Partitioned replay suites cover smoke, 30-case release holdout, multi-turn,
  retrieval, adversarial, and minimized regression cases. Fixtures carry a
  canonical SQL digest, reference date, source/schema metadata, evaluator and
  prompt versions, and a content fingerprint that is regenerated and checked.
- Live quality mode runs Gemini and BigQuery and compares generated output with
  one canonical snapshot per case from the same evaluation run. Candidate and
  reference-query costs are reported separately.
- Quality scoring covers SQL intent, exact row calculations, Retrieval Recall@3,
  MRR, NDCG@3, retrieval usefulness/harm, numeric faithfulness, multi-turn
  intent, expected behavior, operational budgets, and analyst usefulness.
- Repeated live runs report first-attempt, eventual, and per-attempt success,
  p50/p95 latency, confidence intervals, score variance, and flaky cases.
- Reviewer-release latency is reported but does not fail the prototype gate;
  provider-side timing is evaluated separately from correctness, completeness,
  privacy, warehouse execution integrity, and chart-artifact requirements.
- A release candidate requires five live repetitions, complete two-reviewer
  structured scoring, critical-dimension floors, resolved disagreements and
  rejections, and blinded baseline noninferiority before it can pass.
- Live candidates run only on the default branch in a protected environment;
  approval verifies immutable artifact provenance and makes the release decision
  without rerunning Gemini or BigQuery.
- Runtime and evaluation Docker targets prove evaluation dependencies and data
  do not ship in the application image.

See `docs/qa.md` for the exact gates and commands.

## 8. Observability

Prototype JSONL events include trace, conversation and turn IDs, versions,
history size, selected tools and timings, retrieved trio IDs, SQL validation,
warehouse bytes/latency/rows, retries and budgets, chart digest/format/size,
usage, degradation, redactions, failure codes, and terminal status.

Production sends the same fields through OpenTelemetry to Prometheus,
Alertmanager, Grafana, Loki, and Tempo. The HLD defines availability, latency,
dependency-degradation, quality, cost, PII, and ownership objectives and alerts.

## 9. Agility And Persona Management

Prototype tone, formatting, limits, and versioned prompt content remain in
`config/agent.yaml` and packaged resources for reviewer convenience.

Production separates immutable safety instructions from editable persona style.
OIDC-authorized non-developers create drafts, run validation and evaluations,
publish a version pointer without deployment, and roll back. Sessions and traces
pin the persona version used for reproducibility.
