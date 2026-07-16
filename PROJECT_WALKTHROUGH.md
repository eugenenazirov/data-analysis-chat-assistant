# Project Walkthrough

This is the reviewer entrypoint for the working prototype. It explains what is
implemented, where each responsibility lives, how one question is processed,
and which production capabilities remain design-only.

## What The Prototype Proves

The application combines:

- PydanticAI 2.9 for model-selected tools, structured output, validation,
  history processing, and usage limits;
- Gemini for analysis and Golden Knowledge embeddings;
- BigQuery for live retail analytics;
- Qdrant for approved Question/SQL/Analyst Report precedents;
- deterministic SQL, evidence, and privacy policies;
- automatic PNG/SVG chart generation from verified rows;
- a Clean Architecture application core reusable by another inbound adapter.

The CLI is intentionally the only implemented UI. The production HLD specifies
the durable API, identity, persistence, audit, promotion, and administrative
workflows separately.

## Fast Reviewer Path

Start with the complete credential-free gate:

```bash
just check
just container-check
```

With Google credentials configured, prepare the exact current application image
and its dependencies:

```bash
just live-setup
```

Expected outcome: Qdrant becomes healthy, the Golden index is recreated,
diagnostics reports `Revision match: yes`, `Prompt match: yes`, and prompt
`analysis-v12`; five
chart smoke cases produce validated artifacts, including PNG, SVG, pandas,
seaborn, and the 156-cell heatmap.

Then execute the reviewer flow:

```bash
just reviewer-live
```

That command checks a schema-only question, a live analytical question, a PII
request, the broad six-month/category chart, a multi-turn conversation, and a
Qdrant-degraded turn. Run individual probes with `just ask`, `just chat`,
`just diagnostics`, `just chart-smoke`, and `just bq-smoke`. Every command that
uses the application image rebuilds it through Docker's cache first.

Expected evidence and tool patterns:

| Flow | Valid tools |
|---|---|
| Schema introduction | model returns structured schema output; retrieval, SQL, and chart are hidden |
| Simple analytical question | `run_sql_query`; the agent may first use `retrieve_golden_examples` when precedent would help |
| Ranking/time-window/comparison | agent-selected `retrieve_golden_examples` when useful, then `run_sql_query` |
| Chart request | optional retrieval when useful, `run_sql_query`, then `generate_chart` |
| PII-only or unsupported request | none; deterministic refusal or clarification |
| PII request with a clear safe analytical intent | refuse direct identifiers; optional retrieval may precede `run_sql_query`, which may return an aggregate keyed only by approved customer ID |
| Retrieval outage | retrieval returns degraded, SQL continues, report has `degraded=true` and a caveat |

For a chart request, the final report must name a real file and the command must
show `rows_returned`, `rows_available`, and `result=complete`. If more than 500
rows are available, the correct behavior is a deterministic 20-row preview and
a request to narrow the scope; no chart or completeness claim is allowed.
Compose bind-mounts the application artifact directory to the host, so the
reported `artifacts/charts/...` path can be opened directly from the repository.

## One Turn, End To End

1. `retail_agent.presentation.cli.app` parses the command and asks `Runtime` for
   an application operation.
2. `retail_agent.bootstrap.Runtime` supplies the already-composed
   `AnalyzeQuestion` use case.
3. `AnalyzeQuestion` loads or starts a `Conversation`, passes the question,
   bounded prior turns, and user profile through the `AnalysisAgent` port, then
   redacts and persists the completed turn.
4. `PydanticAIAnalysisAgent` translates domain turns into PydanticAI messages.
   Verified SQL rows and chart references from earlier turns remain structured,
   bounded context rather than a single previous-question string. The turn also
   receives one captured UTC reference date so relative SQL periods and numeric
   evidence validation use the same clock value.
5. The PydanticAI `FunctionToolset` registers retrieval, SQL, and chart tools.
   The model chooses retrieval when approved analytical precedent would improve
   the answer and may proceed directly to SQL when it would not. Retrieval is
   bounded to one attempt and is hidden after SQL succeeds. Schema-only requests
   keep structured model output while hiding all three execution tools.
   `generate_chart` remains hidden until `run_sql_query` succeeds in the current
   run.
6. Retrieval returns approved precedents or a typed degraded result. It never
   presents an outage as an ordinary empty match set.
7. SQL passes `sqlglot` validation, table/column allowlists, BigQuery dry run,
   byte cap, and stable job-ID execution. The client fetches at most 500 rows
   without injecting a misleading SQL `LIMIT`; BigQuery's complete row count
   distinguishes complete from partial results. Literal limits up to 500 retain
   their meaning. Failures after submission are
   non-retryable to avoid duplicate warehouse work. A valid empty result is
   preserved; output validation requires an explicit no-matching-data statement
   instead of asking the model to broaden and rerun the query.
8. Chart code, when requested, receives recursively redacted verified rows
   through `input.json` and must create `chart.png` by default or `chart.svg`
   only when SVG was explicitly requested. Syntax, dependency, data-shape,
   output, timeout, and runtime failures return bounded repair instructions.
   Two `ModelRetry` repairs allow three total chart attempts without repeating
   the completed warehouse query; the tool is hidden after that budget.
9. Structured output is one of data analysis, schema explanation,
   clarification, unsupported request, or execution failure. Data output
   requires a query; numeric claims and chart references are validated with a
   bounded output-retry budget.
10. The runtime attaches only the executed SQL and actual chart artifact,
    recursively redacts PII, records structured telemetry, saves the turn, and
    returns a presentation-neutral DTO.

## Package Map

### Domain

`retail_agent/domain/` contains models, errors, and deterministic policies. It
imports no PydanticAI, CLI, SDK, logging-vendor, or settings code.

- `models/conversation.py`: conversation aggregate, value objects, ordered
  turns, bounded retention, and verified tool-result summaries.
- `models/analysis.py`: user-facing report/failure DTOs and discriminated agent
  outputs.
- `models/query.py`: safe SQL, query result, and Golden Knowledge types.
- `models/chart.py`: fixed chart request/artifact contracts.
- `policies/analysis_output.py`: deterministic Markdown-table and row-dump
  rejection shared by structured-output validation.
- `policies/report_evidence.py`: runtime and evaluation numeric-evidence checks.
- `policies/privacy.py`: recursive text/value redaction.
- `policies/retrieval.py`: model-facing retrieval guidance and high-confidence
  schema-tool visibility classification.
- `errors.py`: application-safe analytics, retrieval, and chart errors.

### Application

`retail_agent/application/` coordinates use cases using narrow replaceable
ports.

- `use_cases/analyze_question.py`: one complete conversation turn.
- `use_cases/start_conversation.py` and `clear_conversation.py`: session
  lifecycle without UI assumptions.
- `ports/`: analysis agent, analytics, retrieval, chart executor, conversation
  repository, and telemetry contracts.
- `dto.py`: responses shared by CLI and a future API adapter.

The application layer contains no BigQuery, Qdrant, Gemini, PydanticAI, or Typer
imports.

### Infrastructure

`retail_agent/infrastructure/` implements outbound ports:

- `agents/google_model.py`: one reusable Gemini model with three transport
  attempts for 408, 429, and retryable 5xx responses; Vertex uses two global
  attempts followed by one `us-central1` attempt inside the same model request;
- `agents/pydantic_ai_analysis_agent.py`: domain/PydanticAI history adapter;
- `analytics/bigquery_adapter.py`: SDK exception translation and query boundary;
- `retrieval/qdrant_adapter.py`: Golden Knowledge storage/search;
- `retrieval/gemini_embeddings.py`: Gemini and deterministic hash embedders;
- `charts/local_python_executor.py`: bounded local chart subprocess and
  actionable failure classification;
- `charts/smoke.py`: image-level PNG, SVG, pandas, seaborn, and heatmap proof;
- `conversations/in_memory_repository.py`: session-local prototype persistence;
- `prompts/`: packaged prompt resource plus deterministic builder;
- `settings.py`: nested Pydantic Settings and source precedence;
- `observability.py`: redacted JSONL telemetry and optional Logfire setup.

The chart subprocess limits source, output, and captured-output sizes; uses a
minimal environment and fixed filenames; kills its process group on timeout;
rejects symlinks, malformed formats, and active/external SVG content; and logs a
digest instead of raw code or data. It is not a security sandbox. Production
must replace it with a separately isolated worker.

### Presentation And Composition

- `retail_agent/presentation/cli/app.py`: `ask`, `chat`, `index-golden`,
  `bq-smoke`, `diagnostics`, and `chart-smoke`; no evaluation, prompt, or
  tool-order logic.
- `retail_agent/presentation/cli/renderer.py`: Rich rendering only.
- `retail_agent/bootstrap.py`: the sole composition root for settings and
  concrete adapters.
- root compatibility modules such as `retail_agent.config` preserve stable
  imports while delegating to the new packages.

### Evaluations

`evals/` is outside the production package:

- `guardrails.py`: `pydantic-evals` dataset for SQL/privacy controls;
- `quality.py`: deterministic replay, credentialed repeated-live scoring,
  trajectory telemetry, reliability intervals, reference-query accounting, and
  quality-v8 three-way semantic decisions. Proven CTE/window/formula/alias
  equivalence passes, hard invariant violations fail, and only unresolved
  lineage/result ambiguity produces a release-blocking `REVIEW`;
- `human.py` and `rubrics/`: separate blinded A/B and pointwise review packets,
  structured reviewer calibration, and final release decisions;
- `datasets/smoke.jsonl`: four fast release sentinels;
- `datasets/release_holdout.jsonl`: 30 held-out analytical cases;
- `datasets/multi_turn.jsonl`, `development.jsonl`, `adversarial.jsonl`, and
  `regression.jsonl`: conversation, retrieval, abuse-resistance, and minimized
  failure coverage;
- `datasets/build_replay_fixtures.py`: deterministic fixture generation and
  provenance verification;
- `run.py`: dedicated evaluation CLI.

The runtime dependency uses `pydantic-ai-slim[google]`; `pydantic-evals` is in
the `eval` dependency group. The runtime image copies neither `evals/` nor its
dataset, while the Docker `evaluation` target adds both.

## Safety Boundaries

### SQL and cost

- one read-only BigQuery query only;
- fully qualified allowlisted tables;
- table-specific safe-column allowlists;
- no `SELECT *`, whole-row alias projection, DDL, or DML;
- 500-row client retrieval cap with explicit completeness metadata, dry-run
  bytes, execution timeout, and stable job ID;
- retry feedback only while safe, never after an unknown submitted-job outcome.

### Output and privacy

- successful data output requires verified rows;
- final SQL comes from the query tool, not model prose;
- numeric claims are checked against compatible returned measures;
- Markdown tables and row-by-row narrative copies of verified results are
  rejected within the bounded output-retry budget;
- chart references must match the current chart tool result;
- final DTOs, tool summaries, and telemetry are recursively redacted;
- provider and SDK detail is translated into typed user-safe failures.

### Agent limits

- request, tool-call, token, SQL-retry, chart-retry, output-retry, history-turn, and
  history-byte limits are configured centrally;
- tool calls have explicit timeouts;
- `ProcessHistory` keeps complete recent turn groups;
- large SQL tool results are compacted before reuse.

## Requirement Mapping

| Requirement | Prototype evidence | Production extension |
|---|---|---|
| Hybrid intelligence | Model-selected approved-example retrieval with observable, non-blocking degradation | Human candidate review, versioned index promotion, rollback |
| Safe analytics | AST guard, safe columns, byte/row/time limits, evidence checks | Warehouse policy, workload identity, network controls |
| Multi-turn analysis | Conversation aggregate and complete bounded PydanticAI history | Durable PostgreSQL sessions and summaries |
| Automatic charts | Dynamic post-query tool and local bounded executor | Isolated chart worker with separate credentials/resources |
| Resilience | Typed failures, safe retry boundary, degraded verified-row report | Circuit breakers, HA storage, recovery objectives |
| Observability | Trace/session IDs, versions, tool timings, usage, retries, degradation | OpenTelemetry metrics/logs/traces and alerts |
| Quality assurance | Branch-gated tests, guardrails, 67 partitioned replay cases, repeated-live and human-gate tests, runtime/evaluation image checks | Scheduled canary plus protected release-candidate and analyst approval workflows |
| Saved reports/personas | HLD only | OIDC, PostgreSQL, confirmations, audit, admin UI |

## Configuration And Versions

`config/agent.yaml` is the readable baseline. Environment and `.env` aliases
override it; explicit initialization has highest priority. Credentials use
`SecretStr`. Prompt content is packaged at
`retail_agent/infrastructure/prompts/templates/analysis-v12.md`; the exact tested
chart programs are injected from the shared chart-template module, and the prompt
version is recorded in telemetry and image metadata.

The locked stack uses Python 3.12, uv 0.10.8, PydanticAI 2.9, Pydantic Settings,
BigQuery, Qdrant, Gemini, sqlglot, Matplotlib, NumPy, pandas, seaborn, Rich, and
Typer. Vertex chat defaults to the global endpoint, fails over to `us-central1`
inside the same bounded model request, and keeps embeddings in `us-central1`;
`GOOGLE_CLOUD_LOCATION` remains a compatibility fallback.

## Failure Interpretation

| Symptom | Meaning and next check |
|---|---|
| `Revision match: NO` | The Compose image is stale; rerun `just live-setup`. |
| `chart-smoke` fails | The production image is missing a declared plotting dependency or cannot produce a validated artifact; do not begin model review. |
| `missing_dependency` | The generated program imported a package unavailable in the image; diagnostics lists the supported versions. |
| `syntax_error` / `data_shape_error` | The model receives the failing line, expected filename, and available columns and may repair twice. |
| `rows_available` exceeds `rows_returned` | The result is intentionally partial; narrow the request before interpreting or plotting it. |
| Golden Knowledge caveat | Qdrant degraded; the report may still be valid because SQL was independently verified. |
| Provider 408/429/5xx | The shared transport retries up to three attempts without restarting the agent or repeating completed BigQuery work. |
| Provider 400/401/403 | The request is non-retryable; it fails once with normalized safe telemetry and does not trigger regional fallback. |
| `REVIEW` in an eval | Semantic equivalence could not be proven or disproven. It blocks the suite but is not counted as a Gemini failure. Inspect the stored SQL/result diagnostics. |

For a targeted remediation probe, repeat `--case-id` on the evaluation CLI. It
must use the same dataset, model, reference dates, and provider settings as the
failed run. A subset proves the fix for those cases only; it does not replace
the full 34-case release candidate or two-reviewer packet.

## Known Prototype Boundaries

- Conversation storage is in memory and process-local.
- The CLI has no authentication or authorization layer.
- Saved Reports, destructive confirmation, durable auditing, persona admin, and
  Golden candidate promotion are production-design-only.
- The local chart subprocess cannot defend against determined malicious code.
- Live Gemini/BigQuery behavior requires credentials and is covered by a
  default-branch-only protected scheduled/manual workflow. Release approval
  verifies and consumes the frozen candidate artifact without another live run.

Those boundaries are deliberate and do not change the inward dependency rule or
the reusable application use cases demonstrated by the prototype.
