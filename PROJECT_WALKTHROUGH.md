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

Local deterministic verification:

```bash
uv sync --frozen --all-groups
uv pip check
uv run ruff check .
uv run pytest --cov=retail_agent --cov-branch --cov-fail-under=85
uv run python -m evals.run guardrails
uv run python -m evals.run quality --mode replay
```

Runtime and evaluation images:

```bash
docker build --target runtime -t retail-agent:runtime .
docker run --rm retail-agent:runtime --help
docker build --target evaluation -t retail-agent:evaluation .
docker run --rm retail-agent:evaluation guardrails
docker run --rm retail-agent:evaluation quality --mode replay
```

Credentialed application path:

```bash
docker compose up -d qdrant
docker compose run --rm app index-golden --recreate
docker compose run --rm app ask "Plot monthly revenue by category" --user manager_a
docker compose run --rm app chat --user manager_a
```

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
5. The PydanticAI `FunctionToolset` registers retrieval and SQL. For classified
   ranking, time-window, customer-behavior, return, comparison, and follow-up
   questions, tool preparation exposes retrieval but hides SQL until retrieval
   has been attempted, including a typed degraded attempt. Schema,
   clarification, unsupported, and simple unambiguous requests may expose SQL
   immediately. `generate_chart` remains hidden until `run_sql_query` succeeds
   in the current run.
6. Retrieval returns approved precedents or a typed degraded result. It never
   presents an outage as an ordinary empty match set.
7. SQL passes `sqlglot` validation, table/column allowlists, row limits, BigQuery
   dry run, byte cap, and stable job-ID execution. Failures after submission are
   non-retryable to avoid duplicate warehouse work. A valid empty result is
   preserved; output validation requires an explicit no-matching-data statement
   instead of asking the model to broaden and rerun the query.
8. Chart code, when requested, receives the verified rows through `input.json`
   and must create a fixed PNG or SVG filename in a temporary directory.
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
- `policies/retrieval.py`: shared routing instructions and deterministic
  precedent-required question classification.
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

- `agents/pydantic_ai_analysis_agent.py`: domain/PydanticAI history adapter;
- `analytics/bigquery_adapter.py`: SDK exception translation and query boundary;
- `retrieval/qdrant_adapter.py`: Golden Knowledge storage/search;
- `retrieval/gemini_embeddings.py`: Gemini and deterministic hash embedders;
- `charts/local_python_executor.py`: bounded local chart subprocess;
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

- `retail_agent/presentation/cli/app.py`: `ask`, `chat`, `index-golden`, and
  `bq-smoke`; no evaluation, SDK, prompt, or tool-order logic.
- `retail_agent/presentation/cli/renderer.py`: Rich rendering only.
- `retail_agent/bootstrap.py`: the sole composition root for settings and
  concrete adapters.
- root compatibility modules such as `retail_agent.config` preserve stable
  imports while delegating to the new packages.

### Evaluations

`evals/` is outside the production package:

- `guardrails.py`: `pydantic-evals` dataset for SQL/privacy controls;
- `quality.py`: deterministic replay, credentialed repeated-live scoring,
  trajectory telemetry, reliability intervals, and reference-query accounting;
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
- bounded result rows, dry-run bytes, execution timeout, and stable job ID;
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

- request, tool-call, token, SQL-retry, output-retry, history-turn, and
  history-byte limits are configured centrally;
- tool calls have explicit timeouts;
- `ProcessHistory` keeps complete recent turn groups;
- large SQL tool results are compacted before reuse.

## Requirement Mapping

| Requirement | Prototype evidence | Production extension |
|---|---|---|
| Hybrid intelligence | Conditionally required approved-example retrieval with observable, non-blocking degradation | Human candidate review, versioned index promotion, rollback |
| Safe analytics | AST guard, safe columns, byte/row/time limits, evidence checks | Warehouse policy, workload identity, network controls |
| Multi-turn analysis | Conversation aggregate and complete bounded PydanticAI history | Durable PostgreSQL sessions and summaries |
| Automatic charts | Dynamic post-query tool and local bounded executor | Isolated chart worker with separate credentials/resources |
| Resilience | Typed failures, safe retry boundary, degraded verified-row report | Circuit breakers, HA storage, recovery objectives |
| Observability | Trace/session IDs, versions, tool timings, usage, retries, degradation | OpenTelemetry metrics/logs/traces and alerts |
| Quality assurance | 319 offline tests, guardrails, 67 partitioned replay cases, repeated-live and human-gate tests, runtime/evaluation image checks | Scheduled canary plus protected release-candidate and analyst approval workflows |
| Saved reports/personas | HLD only | OIDC, PostgreSQL, confirmations, audit, admin UI |

## Configuration And Versions

`config/agent.yaml` is the readable baseline. Environment and `.env` aliases
override it; explicit initialization has highest priority. Credentials use
`SecretStr`. Prompt content is packaged at
`retail_agent/infrastructure/prompts/templates/analysis-v4.md`, and its version
is recorded in telemetry.

The locked stack uses Python 3.12, uv 0.10.8, PydanticAI 2.9, Pydantic Settings,
BigQuery, Qdrant, Gemini, sqlglot, Matplotlib, Rich, and Typer.

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
