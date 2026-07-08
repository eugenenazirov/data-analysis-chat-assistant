# Live BigQuery Setup

This prototype can run a live BigQuery smoke test without Gemini or Qdrant:

```bash
docker compose run --rm app bq-smoke
```

The command validates credentials, runs a dry-run with `maximum_bytes_billed`,
executes a small aggregate query against
`bigquery-public-data.thelook_ecommerce.order_items`, and prints the BigQuery job
metadata. It uses the same SQL guardrails as the agent.

## Fastest Personal GCP Setup

Use a personal project you control. From the screenshot, the visible project is
`your-project-id`; replace it if you prefer another project.

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

Google documents that on-demand BigQuery includes the first 1 TiB of query data
processed per month for free. The prototype still protects each query with:

- BigQuery dry-run before execution.
- `maximum_bytes_billed` from `BQ_MAX_BYTES_BILLED`.
- SQL table allowlist.
- PII column denylist.

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
