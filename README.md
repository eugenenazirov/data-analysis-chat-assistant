# Retail Data Analysis Chat Assistant

Working prototype and production high-level design for an executive-facing retail
analytics assistant. The production reference runtime is Kubernetes with portable
OIDC, PostgreSQL, S3-compatible storage, Qdrant, and OpenTelemetry contracts;
BigQuery and Gemini are replaceable adapters.

For a module-by-module reviewer guide and assignment mapping, start with
[PROJECT_WALKTHROUGH.md](PROJECT_WALKTHROUGH.md).

The working prototype uses:

- PydanticAI for typed agent orchestration.
- Gemini for chat and Golden Knowledge embeddings.
- BigQuery for live analytics against `bigquery-public-data.thelook_ecommerce`.
- Qdrant for Golden Knowledge vector retrieval.
- uv for Python 3.12 environments and reproducible locked dependencies.
- Docker Compose for a repeatable reviewer setup.

## Quick Start With Docker

1. Create environment file:

   ```bash
   cp .env.example .env
   ```

2. Fill in `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_QUOTA_PROJECT`.
   `GOOGLE_API_KEY` is optional because the default model path uses Vertex AI
   through Application Default Credentials.

3. Enable APIs and authenticate on the host:

   ```bash
   export PROJECT_ID=your-project-id
   gcloud services enable bigquery.googleapis.com aiplatform.googleapis.com --project "$PROJECT_ID"
   gcloud auth application-default login
   gcloud auth application-default set-quota-project "$PROJECT_ID"
   ```

   The Compose file mounts `~/.config/gcloud` read-only into the app container.
   A service-account JSON can be used instead by changing the mount and
   `GOOGLE_APPLICATION_CREDENTIALS`.

4. Build and start Qdrant:

   ```bash
   docker compose build
   docker compose up -d qdrant
   ```

5. Index Golden Knowledge:

   ```bash
   docker compose run --rm app index-golden --recreate
   ```

6. Ask a question:

   ```bash
   docker compose run --rm app ask "Which product categories drove the most revenue last month?" --user manager_a
   ```

7. Run a live BigQuery smoke test without Gemini or Golden Knowledge retrieval:

   ```bash
   docker compose run --rm app bq-smoke
   ```

8. Run deterministic guardrail evals:

   ```bash
   docker compose run --rm app eval
   ```

For an offline Qdrant smoke test, use deterministic demo embeddings:

```bash
docker compose run --rm -e EMBEDDING_PROVIDER=hash app index-golden --recreate
```

Use `EMBEDDING_PROVIDER=gemini` for the real assignment path. It works with a
Google AI Studio API key or with Vertex AI ADC when `GOOGLE_CLOUD_PROJECT` and
`GOOGLE_CLOUD_LOCATION` are set.

The default reviewer path uses `LLM_MODEL=google-cloud:gemini-2.5-flash` and
`EMBEDDING_MODEL=gemini-embedding-001` through Vertex AI. For Google AI Studio,
set `GOOGLE_API_KEY` and switch `LLM_MODEL` to the corresponding `google:...`
provider model.

## Local Setup With uv

Install [uv](https://docs.astral.sh/uv/), then create the exact locked
environment. Shell activation and pip are not required.

```bash
uv lock --check
uv sync --frozen --all-groups
uv pip check
docker compose up -d qdrant
export QDRANT_URL=http://localhost:6333
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_CLOUD_LOCATION=us-central1
export LLM_MODEL=google-cloud:gemini-2.5-flash
export EMBEDDING_PROVIDER=gemini
uv run python -m retail_agent index-golden --recreate
uv run python -m retail_agent ask "Top products by sales" --user manager_b
```

Python 3.12 and uv 0.10.8 are pinned in the project. `pyproject.toml` is the
dependency source of truth and the committed `uv.lock` is used unchanged by
local setup, CI, and Docker.

## CLI

```bash
uv run python -m retail_agent chat --user manager_a
uv run python -m retail_agent ask "monthly revenue by category" --user manager_a
uv run python -m retail_agent index-golden --recreate
uv run python -m retail_agent eval
uv run python -m retail_agent eval --suite quality --mode replay
```

## Prototype Scope

Implemented:

- Golden Knowledge retrieval from Qdrant.
- Deterministic Golden Knowledge prefetch before each model run.
- Multi-turn chat state using PydanticAI message history and contextual follow-up retrieval.
- Gemini embeddings through Vertex AI ADC or Google AI Studio API key.
- BigQuery SQL generation/execution path through PydanticAI tools.
- SQL AST guardrails with `sqlglot`.
- Safe-column allowlists, PII/whole-row projection blocking, result-limit enforcement, and output redaction.
- Graceful Qdrant degradation and top-level model/warehouse failure boundaries.
- Redacted degraded reports when a model fails after verified query execution.
- Effective configurable SQL retry budgets, dry-run byte caps, and timeouts.
- Turn- and byte-bounded chat history with compacted prior SQL tool results.
- Structured session/turn-aware JSONL events.
- Dockerized app and Qdrant services.
- Deterministic guardrail and structural answer-quality replay evals plus a
  credentialed automated live gate and separate analyst-scored release gate.
- uv-locked Python environment and automated lint/test/eval/Docker CI gates.

Documented in HLD, not coded in prototype v1:

- Destructive Saved Reports deletion flow.
- Durable human-reviewed Golden Knowledge promotion workflow.
- Production session, preference, persona, report, and audit persistence.
- Non-developer admin UI for persona updates.

See [docs/architecture.md](docs/architecture.md), [docs/requirements.md](docs/requirements.md), and [docs/qa.md](docs/qa.md).
For live BigQuery setup, see [docs/bigquery-live-test.md](docs/bigquery-live-test.md).
