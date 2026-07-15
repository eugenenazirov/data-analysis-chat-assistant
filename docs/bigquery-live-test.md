# Live BigQuery Setup

This prototype can run a live BigQuery smoke test without calling Gemini or
Golden Knowledge retrieval:

```bash
docker compose run --rm app bq-smoke
```

The command validates credentials, runs a dry-run with `maximum_bytes_billed`,
executes a small aggregate query against
`bigquery-public-data.thelook_ecommerce.order_items`, and prints the BigQuery job
metadata. It uses the same SQL guardrails as the agent.

## Fastest Personal GCP Setup

Use a personal project you control. Replace `your-project-id` with a project
where you can create BigQuery jobs.

```bash
export PROJECT_ID=your-project-id

gcloud auth login
gcloud config set project "$PROJECT_ID"
gcloud services enable bigquery.googleapis.com --project "$PROJECT_ID"

gcloud auth application-default login
gcloud auth application-default set-quota-project "$PROJECT_ID"
```

Create `.env`:

```bash
cp .env.example .env
```

Set:

```dotenv
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_QUOTA_PROJECT=your-project-id
BIGQUERY_LOCATION=US
BQ_MAX_BYTES_BILLED=200000000
```

Then run:

```bash
docker compose build
docker compose run --rm app bq-smoke
```

## Full Agent Test Without AI Studio API Key

If you have a GCP project but no Google AI Studio API key, you can run the
agent and Gemini embeddings through Vertex AI using the same ADC credentials:

```bash
gcloud services enable aiplatform.googleapis.com --project "$PROJECT_ID"
```

Set these values in `.env`:

```dotenv
GOOGLE_CLOUD_LLM_LOCATION=global
GOOGLE_CLOUD_EMBEDDING_LOCATION=us-central1
LLM_MODEL=google-cloud:gemini-2.5-flash
EMBEDDING_PROVIDER=gemini
EMBEDDING_MODEL=gemini-embedding-001
```

Then run:

```bash
just live-setup
just ask "What are the top 5 product categories by gross sales?" manager_a
docker compose down
```

Use `EMBEDDING_PROVIDER=hash` only for an offline Qdrant smoke test that avoids
Gemini embedding calls. The real assignment path is `EMBEDDING_PROVIDER=gemini`,
either through Vertex ADC or a Google AI Studio `GOOGLE_API_KEY`.

## Interviewer Reproducibility

The interviewer does not need your Google credentials. They can use their own
GCP project and run the same commands above. Requirements:

- A Google Cloud project with billing enabled or BigQuery sandbox support.
- BigQuery API enabled.
- Permission to create BigQuery jobs in their project, usually
  `roles/bigquery.jobUser`.
- For user ADC quota/billing, permission to use the project as a quota project,
  normally covered by project owner/editor on their own project or
  `roles/serviceusage.serviceUsageConsumer`.

No dataset-specific permission is required for the assignment data because
`bigquery-public-data.thelook_ecommerce` is public. Their own project is used
only to create and bill/query jobs.

## Cost Control

Billing and free-tier terms can change, so check the current Google Cloud terms
for the project running the demo. Independently of account pricing, the
prototype protects each query with:

- BigQuery dry-run before execution.
- `maximum_bytes_billed` from `BQ_MAX_BYTES_BILLED`.
- SQL table allowlist.
- Table-specific safe-column allowlists, PII column denylist, and whole-row
  projection blocking.
- A 500-row client retrieval cap with exact complete/partial metadata; SQL
  `LIMIT` is not used as a cost control.

For extra safety during demos:

```bash
export BQ_MAX_BYTES_BILLED=50000000
docker compose run --rm app bq-smoke
```

## Service Account Alternative

Do not commit service-account JSON into the repo. If an interviewer wants a
service account instead of user ADC:

1. Create a service account in their project.
2. Grant it `roles/bigquery.jobUser`.
3. Download the JSON key or use workload identity in a managed environment.
4. Mount the key and set `GOOGLE_APPLICATION_CREDENTIALS` to that mounted path.

The app code does not change because the Google client library reads
Application Default Credentials.
