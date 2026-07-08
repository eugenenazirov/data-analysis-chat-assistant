# Retail Data Analysis Chat Assistant

Prototype and high-level design for an executive-facing retail analytics chat assistant.

The working prototype uses:

- PydanticAI for typed agent orchestration.
- Gemini for chat and Golden Knowledge embeddings.
- BigQuery for live analytics against `bigquery-public-data.thelook_ecommerce`.
- Qdrant for Golden Knowledge vector retrieval.
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

## Local Python Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d qdrant
export QDRANT_URL=http://localhost:6333
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_CLOUD_LOCATION=us-central1
export LLM_MODEL=google-cloud:gemini-2.5-flash
export EMBEDDING_PROVIDER=gemini
python -m retail_agent index-golden --recreate
python -m retail_agent ask "Top products by sales" --user manager_b
```

## CLI

```bash
python -m retail_agent chat --user manager_a
python -m retail_agent ask "monthly revenue by category" --user manager_a
python -m retail_agent index-golden --recreate
python -m retail_agent eval
```

## Prototype Scope

Implemented:

- Golden Knowledge retrieval from Qdrant.
- Deterministic Golden Knowledge prefetch before each model run.
- Gemini embeddings through Vertex AI ADC or Google AI Studio API key.
- BigQuery SQL generation/execution path through PydanticAI tools.
- SQL AST guardrails with `sqlglot`.
- PII column blocking and output redaction.
- Dry-run BigQuery cost cap, timeout, and retry feedback.
- Structured local JSONL run logs.
- Dockerized app and Qdrant services.
- Deterministic guardrail evals and pytest suite.

Documented in HLD, not coded in prototype v1:

- Destructive Saved Reports deletion flow.
- Human-reviewed Golden Knowledge promotion workflow.
- Non-developer admin UI for persona updates.

See [docs/architecture.md](docs/architecture.md), [docs/requirements.md](docs/requirements.md), and [docs/qa.md](docs/qa.md).
For live BigQuery setup, see [docs/bigquery-live-test.md](docs/bigquery-live-test.md).
