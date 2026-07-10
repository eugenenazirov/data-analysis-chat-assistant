# Project Walkthrough

This document is a reviewer-oriented map of the Retail Data Analysis Chat
Assistant prototype. It explains what each module does and how the code and
docs satisfy the original `TECHNICAL_ASSIGNMENT.md` requirements.

## What This Project Is

The assignment asks for a production-ready high-level design plus a working
prototype of an executive-facing retail analytics assistant. Managers can ask
natural-language questions about sales, products, orders, customer behavior, or
database structure. The prototype turns those questions into guarded BigQuery
SQL, runs the query against `bigquery-public-data.thelook_ecommerce`, and
returns a structured executive report.

The implementation uses:

- `PydanticAI` for typed agent orchestration and structured reports.
- Google Gemini for chat generation.
- Gemini embeddings for Golden Knowledge retrieval.
- BigQuery for the live public retail dataset.
- Qdrant as the vector store for analyst-approved Golden Knowledge trios.
- Docker Compose to make the reviewer path repeatable.

## Main User Flows

### Docker Reviewer Path

```bash
cp .env.example .env
docker compose build
docker compose up -d qdrant
docker compose run --rm app index-golden --recreate
docker compose run --rm app ask "Which product categories drove the most revenue last month?" --user manager_a
docker compose run --rm app eval
```

### Local Python Path

```bash
uv lock --check
uv sync --frozen --all-groups
uv run python -m retail_agent eval
uv run python -m retail_agent eval --suite quality --mode replay
```

For live BigQuery and Gemini setup, see `README.md` and
`docs/bigquery-live-test.md`.

## Request Lifecycle

1. `retail_agent/cli.py` receives `ask` or `chat` input.
2. `retail_agent/bootstrap.py` creates shared runtime objects: config, logger,
   BigQuery runner, embedder, and Qdrant-backed Golden Store.
3. `retail_agent/agent.py` creates a session-aware turn, trace ID, and loads the
   user's formatting preferences from `config/agent.yaml`.
4. The app retrieves similar Golden Knowledge trios from Qdrant through
   `retail_agent/golden_store.py`. If Qdrant is unavailable, `ask` and `chat`
   continue without retrieved precedent and log the degradation.
5. The PydanticAI agent receives the question, bounded conversation history,
   schema context, user preference, and retrieved Golden Knowledge precedent.
6. When the model proposes SQL, `retail_agent/bigquery.py` sends it through
   `retail_agent/sql_guard.py` before BigQuery sees it.
7. The BigQuery runner performs a dry-run with byte limits, then executes the
   safe query with timeout and job labels.
8. The model turns returned rows into an `AnalysisReport`.
9. `retail_agent/pii.py` redacts email and phone patterns from final output and
   logs.
10. `retail_agent/rendering.py` prints the report with Rich tables and Markdown.
11. The turn returns updated conversation state so `chat` can pass PydanticAI
    message history into the next turn.
12. A top-level failure boundary returns a typed failure or verified degraded
    report instead of terminating the CLI.
13. `retail_agent/observability.py` writes session/turn-aware JSONL events under
    `logs/`.

## Module By Module

### `retail_agent/cli.py`

Owns the Typer command-line interface:

- `ask`: one-shot analytics question.
- `chat`: stateful interactive session with bounded message history.
- `index-golden`: loads seed trios and writes them to Qdrant.
- `bq-smoke`: live BigQuery smoke test without Gemini or Golden Knowledge.
- `eval`: deterministic guardrail evals.

Important behavior: `index-golden` requires Qdrant readiness, while `ask` and
`chat` can continue when retrieval is unavailable.

### `retail_agent/bootstrap.py`

Builds runtime dependencies once from config:

- `EventLogger`
- `BigQueryRunner`
- `GeminiEmbedder` or deterministic `HashingEmbedder`
- `GoldenStore`

It also builds the PydanticAI agent with the configured tool retry budget. This
keeps CLI commands small and makes dependency wiring easy to test.

### `retail_agent/config.py` and `config/agent.yaml`

Define all runtime configuration:

- BigQuery dataset, allowed tables, byte cap, timeout, max rows.
- Qdrant URL and collection name.
- LLM and embedding model names.
- SQL retry budget plus chat-history turn and serialized-byte limits.
- Persona tone and per-user output preferences.
- PII denylist and table-specific safe-column allowlists.
- Local JSONL log path.

Environment variables override the YAML values for Docker and reviewer setup.

### `retail_agent/models.py`

Defines Pydantic models shared across the system:

- `UserProfile`: manager-specific report format and tone.
- `GoldenTrio`: raw Question -> SQL -> Analyst Report seed record.
- `RetrievedTrio`: Qdrant search result passed to the agent.
- `QueryResult`: SQL execution result from BigQuery.
- `AnalysisReport`: structured final agent output.
- `AgentFailure`: typed user-safe failure without provider details.

These typed contracts are one reason the prototype is easier to test and extend.

### `retail_agent/agent.py`

Owns PydanticAI orchestration:

- Builds an agent whose tool retry budget comes from runtime configuration.
- Defines the executive-facing assistant instructions.
- Adds runtime context: trace ID, user profile, schema description, and tone.
- Prefetches Golden Knowledge before the model run.
- Exposes `run_sql_query` as a tool with retry feedback through `ModelRetry`.
- Carries recent PydanticAI messages across chat turns and contextualizes
  retrieval for follow-up questions. Large SQL tool returns are compacted and
  history is bounded by both turns and serialized bytes.
- Converts top-level failures into typed failures or redacted degraded reports.
- Sanitizes the final report and always replaces model-supplied SQL with the
  verified statement that actually executed.

This module is where Hybrid Intelligence, personalization, SQL self-healing, and
final report structure meet.

### `retail_agent/golden_store.py`

Owns Golden Knowledge storage and retrieval:

- Loads seed trios from `data/golden_trios.jsonl`.
- Embeds trios.
- Creates or recreates a Qdrant collection.
- Upserts trios with metadata payloads.
- Searches top-k similar trios for each user question.
- Logs retrieved trio IDs and scores.

In the HLD, future approved analyst reports would flow back into this same raw
trio store and vector index after human review.

### `retail_agent/embeddings.py`

Provides embedding implementations:

- `GeminiEmbedder`: production/reviewer path using Gemini embeddings via
  Google AI Studio API key or Vertex AI Application Default Credentials.
- `HashingEmbedder`: deterministic offline embedder for tests and local smoke
  runs without network calls.

This separation keeps Qdrant behavior testable without live Gemini credentials.

### `retail_agent/bigquery.py`

Owns live warehouse access:

- Lazily creates a `google.cloud.bigquery.Client`.
- Describes allowed table schemas for prompt context.
- Runs SQL validation before BigQuery.
- Performs dry-runs with `maximum_bytes_billed`.
- Executes read-only query jobs with timeout and labels.
- Logs validation, dry-run, cost, latency, row count, and failure events.

The public dataset is fixed to the assignment tables by config and guardrails.

### `retail_agent/sql_guard.py`

Owns deterministic SQL safety:

- Parses SQL with `sqlglot` in BigQuery dialect.
- Allows only one `SELECT` or `UNION` statement.
- Blocks DML and DDL.
- Blocks `SELECT *`, except `COUNT(*)`.
- Requires fully qualified allowed tables from
  `bigquery-public-data.thelook_ecommerce`.
- Applies table-specific safe-column allowlists.
- Blocks configured PII columns.
- Blocks whole-row/table-alias projection such as `SELECT u`,
  `TO_JSON_STRING(u)`, and `ARRAY_AGG(u)`.
- Allows legitimate aggregate aliases that match table names, such as
  `COUNT(*) AS orders ORDER BY orders DESC`.
- Adds a missing `LIMIT` and rejects limits above the configured max rows.

This module is the main deterministic safety boundary before any warehouse call.

### `retail_agent/pii.py`

Provides recursive final-output and log redaction:

- Email patterns become `[REDACTED_EMAIL]`.
- Phone-like patterns become `[REDACTED_PHONE]`.
- Redaction works for strings, lists, and dictionaries.

This is deliberately layered after SQL guardrails, so accidental tool output or
model output is still sanitized.

### `retail_agent/observability.py`

Writes one JSON object per event:

- `trace_id`
- timestamp
- event name
- redaction count
- event-specific fields

It also supports optional Logfire/PydanticAI instrumentation when configured,
while keeping local JSONL logs as the reliable default.

### `retail_agent/evals.py`

Defines deterministic `pydantic-evals` guardrail checks:

- Safe aggregate SQL allowed.
- PII SQL blocked.
- Row projection blocked.
- Excessive limits blocked.
- DML blocked.
- Out-of-scope tables blocked.
- Malformed SQL converted into retryable safety feedback.
- Output PII redacted.
- Missing limits added.

These evals run without live BigQuery or Gemini credentials.

### `retail_agent/quality_evals.py`

Loads the versioned answer-quality dataset and scores:

- AST-level intent-to-SQL contracts.
- Candidate/canonical calculation accuracy.
- Golden Knowledge Retrieval Recall@3 and mean reciprocal rank.
- Lineage-aware support for numerical report claims; derivations only compare
  values from the same measure.
- Multi-turn history use plus structural resolution against contextual canonical SQL.
- Analyst-scored executive usefulness.

Replay mode is deterministic for CI. Live mode runs generated and canonical
queries against the same BigQuery data and writes a machine-readable report.

### `retail_agent/rendering.py`

Formats `AnalysisReport` for the terminal using Rich:

- Refusals are clearly marked.
- Answers, highlights, assumptions, caveats, follow-ups, SQL, and tables are
  printed consistently.
- The trace ID is always shown for debugging.

### `tests/`

The test suite covers the deterministic pieces:

- Agent orchestration and Qdrant fallback.
- BigQuery runner behavior with mocked clients.
- Config loading.
- Gemini embedding client behavior with mocked clients.
- Golden Knowledge indexing/search with deterministic embeddings.
- PII redaction.
- SQL guard allow/block behavior.
- CLI eval behavior.
- Pydantic eval dataset construction.

### Docker And Project Files

- `Dockerfile`: Python slim image, exact uv binary, frozen production dependency
  sync, non-root runtime user, and CLI entrypoint.
- `compose.yaml`: `app` plus Qdrant service, `.env` support, mounted logs, and
  host Google ADC mount for local reviewer use.
- `.env.example`: generic environment template with no secrets.
- `pyproject.toml` and `uv.lock`: single dependency source and complete frozen
  resolution for Python 3.12.
- `data/golden_trios.jsonl`: seed analyst trios for Golden Knowledge.

## Requirement Mapping

### 1. Hybrid Intelligence

Implemented in prototype:

- `data/golden_trios.jsonl` stores seed Question -> SQL -> Report examples.
- `retail_agent/golden_store.py` indexes these trios in Qdrant.
- `retail_agent/agent.py` retrieves top trios before the model run and injects
  them as analyst precedent.
- Logs record retrieved trio IDs and scores.

Production HLD:

- `docs/requirements.md` describes the human-reviewed promotion loop for new
  approved reports.

### 2. Safety And PII Masking

Implemented in prototype:

- Prompt rules refuse non-analytics and PII-seeking requests.
- `sql_guard.py` enforces read-only SQL, allowed tables, safe columns, no row
  projection, PII column blocks, and row limits.
- `pii.py` redacts email and phone patterns from final outputs and logs.
- `bigquery.py` uses dry-runs and byte caps before execution.

Production HLD:

- Add warehouse-level column policies, masking, least-privilege service
  accounts, and reviewable guardrail changes.

### 3. High-Stakes Oversight For Destructive Saved Reports

Documented, not coded in prototype v1:

- `docs/requirements.md` describes a separate report-service workflow with
  ownership checks, preview, typed confirmation token, audit event, and durable
  state.

Reason:

- The assignment only requires the prototype to support at least two listed
  requirements in code. This prototype codes Safety, Resilience, QA,
  Observability, and Hybrid Intelligence. Destructive saved-report deletion is
  intentionally HLD-only.

### 4. Continuous Improvement

Implemented in prototype:

- Per-user preferences in `config/agent.yaml`, for example Manager A table
  output and Manager B bullet output.
- Retrieved Golden Knowledge influences SQL/report style.

Production HLD:

- Approved high-quality interactions become new Golden Knowledge only after
  human review.

### 5. Resilience And Graceful Error Handling

Implemented in prototype:

- `run_sql_query` uses PydanticAI `ModelRetry` for SQL validation/runtime
  failures and empty result sets.
- `max_sql_retries` configures the actual inherited tool retry budget.
- BigQuery dry-runs catch cost and syntax issues before execution.
- Query timeout and max bytes limit protect cost and UX.
- Qdrant failure does not break `ask` or `chat`; the run continues without
  retrieved precedent and logs `golden_knowledge_unavailable`.
- Gemini/provider failure returns a typed error without a traceback; if a query
  already completed, safe rows are returned as a redacted degraded report.

### 6. Quality Assurance

Implemented in prototype:

- `pytest` covers deterministic behavior and mocked integrations.
- `uv run python -m retail_agent eval` runs deterministic guardrail evals.
- `eval --suite quality --mode replay` runs executable intent, calculation,
  retrieval, faithfulness, multi-turn, and usefulness gates.
- `eval --suite quality --mode live` compares generated and canonical BigQuery
  results, safely retries bounded pre-tool transient failures, and requires
  analyst usefulness scoring for release.
- Docker eval verifies the reviewer container path.

Production extends the same versioned dataset with scheduled credentialed runs,
analyst scoring, and model/prompt/persona/index regression dashboards.

### 7. Observability

Implemented in prototype:

- JSONL events include session/turn/trace IDs, user, model, history size,
  retrieved trio IDs, SQL validation status, dry-run bytes, BigQuery latency,
  row count, retry feedback/budget, failure code, degraded status, redaction
  count, and completion status.
- Final CLI output includes trace ID.
- Optional Logfire/OpenTelemetry hooks are available.

### 8. Agility And Persona Management

Implemented in prototype:

- `config/agent.yaml` controls persona tone and user formatting preferences
  without code changes.

Production HLD:

- Move persona instructions to an admin-editable config store with validation,
  versioning, approval, and rollback.

## What To Demo In Review

1. Start with `README.md` for setup.
2. Open `docs/architecture.md` for diagrams.
3. Open this file for module-by-module orientation.
4. Run deterministic checks:

   ```bash
    uv run python -m retail_agent eval
    uv run python -m retail_agent eval --suite quality --mode replay
    uv run pytest
   ```

5. If credentials are available, run the live Docker path:

   ```bash
   docker compose build
   docker compose up -d qdrant
   docker compose run --rm app index-golden --recreate
   docker compose run --rm app bq-smoke
   docker compose run --rm app ask "Which product categories drove the most revenue last month?" --user manager_a
   docker compose down
   ```

## Known Prototype Boundaries

- The saved-report destructive workflow is production HLD only.
- Production session/preferences/reports/personas remain HLD-only; the prototype
  intentionally stores only in-memory CLI conversation state.
- The raw Golden Knowledge promotion workflow is represented by seed JSONL and
  production design, not a full analyst review UI.
- BigQuery is the only implemented warehouse runner, but the boundary is narrow:
  `BigQueryRunner` can be replaced by another warehouse runner.
- The CLI is intentionally simple because the assignment does not require a UI.
