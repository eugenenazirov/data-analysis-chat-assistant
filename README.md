# Retail Data Analysis Chat Assistant

An executive-facing retail analytics assistant with a working CLI prototype and
a production high-level design. The runtime uses Clean Architecture boundaries,
PydanticAI 2.9 structured agents, BigQuery, Qdrant, Gemini, bounded multi-turn
history, evidence validation, and automatic chart generation.

For a reviewer-oriented code tour, start with
[PROJECT_WALKTHROUGH.md](PROJECT_WALKTHROUGH.md).

## Command Shortcuts

If [`just`](https://just.systems/) is installed, the root `justfile` provides
shortcuts for the common developer and reviewer workflows. Run `just` to list
every available recipe.

```bash
just setup
just check
just review
just ask "Plot monthly revenue by category"
```

`just check` is the complete credential-free local gate. `just review` also
builds and verifies the runtime and evaluation images. The sections below keep
the underlying commands available for readers who do not use `just`.

## Runtime Architecture

```text
CLI / future API
        ↓
Application use cases and ports
        ↓
Domain models and deterministic policies
        ↑
Infrastructure adapters (Gemini, BigQuery, Qdrant, charts, telemetry)
```

`retail_agent/bootstrap.py` is the composition root. The CLI only parses input,
selects a conversation, calls use cases, renders DTOs, and maps exit codes. A
future HTTP adapter can invoke the same `AnalyzeQuestion` use case.

During a turn, the PydanticAI agent chooses among these bounded tools:

- `retrieve_golden_examples`: the versioned routing prompt and deterministic
  tool-visibility policy require analyst-approved precedent for rankings, time
  windows, customer behavior, returns, comparisons, and follow-up cohorts;
  schema, clarification, unsupported, and simple unambiguous requests may skip
  it;
- `run_sql_query`: guarded, dry-run-checked, read-only BigQuery execution;
- `generate_chart`: dynamically hidden until the current turn has verified rows.

Data answers use a discriminated structured output and require successful SQL.
The runtime attaches only the SQL actually executed, checks numeric claims
against returned rows, rejects narrative tables/row dumps, validates chart
references, then applies deterministic PII redaction.

## Quick Start With Docker

1. Prepare configuration:

   ```bash
   cp .env.example .env
   ```

2. Set `GOOGLE_CLOUD_PROJECT` and authenticate with Application Default
   Credentials. `GOOGLE_API_KEY` is optional when using the default Vertex AI
   model path.

   ```bash
   export PROJECT_ID=your-project-id
   gcloud services enable bigquery.googleapis.com aiplatform.googleapis.com --project "$PROJECT_ID"
   gcloud auth application-default login
   gcloud auth application-default set-quota-project "$PROJECT_ID"
   ```

3. Build the runtime and start Qdrant:

   ```bash
   docker compose build app
   docker compose up -d qdrant
   ```

4. Index approved Golden Knowledge:

   ```bash
   docker compose run --rm app index-golden --recreate
   ```

5. Ask a question or start a conversation:

   ```bash
   docker compose run --rm app ask "Plot monthly revenue by category" --user manager_a
   docker compose run --rm app chat --user manager_a
   ```

6. Run a live BigQuery smoke test without Gemini or Qdrant retrieval:

   ```bash
   docker compose run --rm app bq-smoke
   ```

Chart artifacts persist in the Compose `chart_artifacts` volume. Local uv runs
write them to `artifacts/charts/` by default.

For an offline Qdrant smoke test, use deterministic demo embeddings:

```bash
docker compose run --rm -e EMBEDDING_PROVIDER=hash app index-golden --recreate
```

## Local Setup With uv

Python 3.12 and uv 0.10.8 are pinned. The lockfile is shared by local, CI, and
container builds.

```bash
uv lock --check
uv sync --frozen --all-groups
uv pip check
docker compose up -d qdrant
export QDRANT_URL=http://localhost:6333
uv run python -m retail_agent index-golden --recreate
uv run python -m retail_agent ask "Top products by sales" --user manager_b
```

The runtime CLI contains only application commands:

```bash
uv run python -m retail_agent ask "monthly revenue by category" --user manager_a
uv run python -m retail_agent chat --user manager_a
uv run python -m retail_agent index-golden --recreate
uv run python -m retail_agent bq-smoke
```

## Evaluations

Evaluation code, its dataset, and `pydantic-evals` are separate from the runtime
package and image.

```bash
uv run python -m evals.run guardrails
uv run python -m evals.run quality --mode replay
```

The dedicated container target provides the same entrypoint:

```bash
docker build --target evaluation -t retail-agent-evaluation:local .
docker run --rm retail-agent-evaluation:local guardrails
docker run --rm retail-agent-evaluation:local quality --mode replay
```

See [docs/qa.md](docs/qa.md) for live evaluation, analyst scoring, thresholds,
and the complete acceptance matrix.

## Configuration

Settings precedence is:

```text
explicit initialization → environment → .env → YAML → safe defaults
```

`config/agent.yaml` defines nested model, BigQuery, retrieval, agent-limit,
conversation, chart, safety, and observability settings. Credentials use
`SecretStr` and are masked when settings are serialized.

Common environment aliases include:

| Area | Variables |
|---|---|
| Gemini | `LLM_MODEL`, `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `GOOGLE_API_KEY`, `GOOGLE_CLOUD_LOCATION` |
| BigQuery | `GOOGLE_CLOUD_PROJECT`, `BIGQUERY_LOCATION`, `BQ_MAX_BYTES_BILLED` |
| Retrieval | `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_COLLECTION`, `GOLDEN_TOP_K` |
| Agent limits | `MAX_AGENT_REQUESTS`, `MAX_TOOL_CALLS`, `MAX_AGENT_TOKENS`, `MAX_SQL_RETRIES`, `MAX_OUTPUT_RETRIES` |
| Conversation | `MAX_CHAT_HISTORY_TURNS`, `MAX_CHAT_HISTORY_BYTES` |
| Charts | `CHART_TIMEOUT_SECONDS` |
| Telemetry | `AGENT_LOG_PATH`, `LOGFIRE_TOKEN` |

## Chart Execution Security

The prototype executes model-generated chart Python automatically in a
short-lived subprocess with a minimal environment, fixed input/output names,
strict source/output/capture limits, and a timeout. It supplies only the current
verified query rows through `input.json` and accepts validated PNG or passive
SVG output.

This subprocess is a reliability boundary, not a security sandbox. Production
must run generated code in an isolated external worker outside the application
container, with separate credentials, filesystem, network policy, CPU, memory,
and time limits.

## Verification

```bash
uv lock --check
uv pip check
uv run ruff check .
uv run pytest --cov=retail_agent --cov-branch --cov-fail-under=85
uv run python -m evals.run guardrails
uv run python -m evals.run quality --mode replay
docker build --target runtime -t retail-agent:runtime .
docker build --target evaluation -t retail-agent:evaluation .
```

The runtime image intentionally excludes `evals/`, its datasets, and
`pydantic-evals`.

## Scope

Implemented in the prototype:

- Clean Architecture packages and import-boundary tests;
- typed settings, packaged prompts, composition root, and thin CLI;
- versioned conditional-retrieval instructions backed by deterministic SQL-tool
  visibility, plus model-selected SQL/chart tools with timeouts and usage
  budgets; retrieval outages degrade without blocking SQL;
- multi-turn conversation state with compacted verified tool context;
- SQL AST guardrails, safe-column allowlists, cost caps, stable job IDs, and
  post-submission outcome protection;
- structured output, evidence validation, PII redaction, degraded dependency
  handling, and structured telemetry;
- local automatic chart execution and separate runtime/evaluation images.

The production design—not this CLI prototype—covers durable OIDC-authenticated
APIs, PostgreSQL persistence, saved reports, destructive confirmation, audit
exports, human Golden Knowledge promotion, and persona administration. See
[docs/architecture.md](docs/architecture.md) and
[docs/requirements.md](docs/requirements.md).
