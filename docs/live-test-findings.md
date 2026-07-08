# Live Test Findings

## Environment

- Date: 2026-07-08
- Gcloud personal config: `personal-delta-smile`
- Gcloud personal account: `<personal-account@example.com>`
- Gcloud personal project: `your-project-id`
- Target dataset: `bigquery-public-data.thelook_ecommerce`
- Local `.env`: configured for project `your-project-id`

## Findings

### 1. Personal gcloud config exists

Status: Pass

The machine has a dedicated personal gcloud configuration:

```text
personal-delta-smile
  account: <personal-account@example.com>
  project: your-project-id
```

### 2. Active gcloud config switched to personal project

Status: Pass

The active gcloud configuration was switched from the work account to
`personal-delta-smile`.

### 3. BigQuery API enablement

Status: Pass

`bigquery.googleapis.com` was enabled for `your-project-id`.

### 4. Application Default Credentials refresh

Status: Pass after retry with browser consent

Attempted command:

```bash
gcloud auth application-default login <personal-account@example.com> --project your-project-id
```

Observed result:

```text
ERROR: There was a problem with web authentication. Try running again with --no-browser.
ERROR: https://www.googleapis.com/auth/cloud-platform scope is required but not consented.
```

Resolution:

The OAuth flow was retried and consent was granted in the browser.

Successful commands:

```bash
gcloud auth application-default login <personal-account@example.com> --project your-project-id
gcloud auth application-default set-quota-project your-project-id
```

Verified ADC state:

```text
type: authorized_user
quota_project_id: your-project-id
has_refresh_token: True
```

### 5. Live BigQuery smoke test from local Python

Status: Pass

Command:

```bash
.venv/bin/python -m retail_agent bq-smoke
```

Evidence:

```text
Dry-run bytes: 4340064
Rows returned: 1
order_item_rows: 180836
distinct_orders: 124626
gross_item_sales: 10740550.12
trace_id: 0301ab58c3c043c39073d67df91eb2d3
```

### 6. Live BigQuery smoke test from Docker

Status: Pass

Command:

```bash
docker compose run --rm app bq-smoke
```

Evidence:

```text
Project: your-project-id
Dry-run bytes: 4340064
Rows returned: 1
order_item_rows: 180836
distinct_orders: 124626
gross_item_sales: 10740550.12
trace_id: d3deebb461de49afb2e8bb7e717e2521
```

### 7. Golden Knowledge indexing from Docker

Status: Pass

Command:

```bash
docker compose run --rm app index-golden --recreate
```

The local `.env` used `EMBEDDING_PROVIDER=hash` for a no-API-key test path.

Evidence:

```text
Indexed 5 Golden Knowledge trios.
```

### 8. Full agent path with BigQuery

Status: Pass

The local `.env` used:

```dotenv
LLM_MODEL=google-cloud:gemini-2.5-flash
EMBEDDING_PROVIDER=hash
GOOGLE_CLOUD_LOCATION=us-central1
```

This lets the agent use Vertex Gemini through ADC instead of requiring a Google
AI Studio API key.

Commands:

```bash
gcloud services enable aiplatform.googleapis.com --project your-project-id
docker compose up -d qdrant
docker compose run --rm app index-golden --recreate
docker compose run --rm app ask "What are the top 5 product categories by gross sales?" --user manager_a
```

Evidence:

```text
Answer: The top 5 product categories by gross sales are presented below.
Top category: Outerwear & Coats, gross sales 1330431.52
Second category: Jeans, gross sales 1249292.35
trace_id: be39e332b59042458f8e9fcc059cc15e
```

Generated SQL:

```sql
SELECT
    p.category,
    SUM(oi.sale_price) AS gross_sales
FROM
    `bigquery-public-data.thelook_ecommerce.order_items` AS oi
    JOIN `bigquery-public-data.thelook_ecommerce.products` AS p
      ON oi.product_id = p.id
GROUP BY
    p.category
ORDER BY
    gross_sales DESC
LIMIT 5
```

### 9. Guardrail evals from Docker

Status: Pass

Command:

```bash
docker compose run --rm app eval
```

All guardrail evals passed:

- safe aggregate SQL accepted
- PII SQL blocked
- destructive SQL blocked
- output PII redacted
- missing query limit added

## Bugs Found

No application bugs found in the live BigQuery pass.

Notes:

- Initial ADC login failed because the OAuth browser flow did not consent to
  `cloud-platform`; retrying and granting consent resolved it.
- Full agent testing without a Google AI Studio API key is possible via
  `google-cloud:gemini-2.5-flash` after enabling Vertex AI.

## Post-Review Guardrail Fixes

Status: Fixed

After the live pass, a code review found three deterministic guardrail gaps:

- Table allowlisting checked only the table basename, so a table named `orders`
  in another project or dataset could pass validation.
- The configured PII denylist did not cover all sensitive fields present in
  `bigquery-public-data.thelook_ecommerce.users`, including names, exact
  address, postal code, latitude, longitude, and `user_geom`.
- Malformed SQL parse errors were not wrapped as `SQLSafetyError`, so they could
  bypass the PydanticAI `ModelRetry` path.

Regression coverage was added for each case. The guardrail eval runner now uses
`pydantic-evals` and includes explicit cases for table scope, user PII, and
malformed SQL.

## Post-Review Golden Knowledge Orchestration Fix

Status: Fixed

The first full-agent retest used a real Gemini chat model and live BigQuery, but
the model chose to call only the SQL tool and skipped the optional Golden
Knowledge retrieval tool. Retrieval is now deterministic in the app
orchestration layer: the app retrieves top Golden Trios from Qdrant before the
model call, injects them into the prompt as analyst precedent, and logs the
retrieved trio IDs on completion.

Verification command:

```bash
docker compose run --rm app ask "Which product categories drove the most revenue last month?" --user manager_a
```

Evidence from trace `267a12cd4cca49cc854659467692312c`:

```text
golden_knowledge_retrieved ids:
  trio_monthly_revenue_category
  trio_customer_behavior_no_pii
  trio_underperforming_branch_proxy
agent_golden_context_prepared: same ids
bigquery_query_succeeded: rows=10, tables=["order_items", "products"]
agent_run_completed: retrieved_trio_ids recorded
```

The returned answer followed the retrieved revenue-category precedent: it
excluded cancelled/returned items and included order count as context.

## Post-Review Gemini Embedding Live Test

Status: Pass

The embedding path now supports Vertex AI explicitly when `GOOGLE_API_KEY` is
empty. In that mode, the app creates `genai.Client(vertexai=True, project=...,
location=...)` and uses `gemini-embedding-001` for Golden Knowledge indexing and
retrieval.

Verification commands:

```bash
docker compose build app
docker compose up -d qdrant
docker compose run --rm -e EMBEDDING_PROVIDER=gemini app index-golden --recreate
docker compose run --rm -e EMBEDDING_PROVIDER=gemini app ask "Which product categories drove the most revenue last month?" --user manager_a
```

Evidence:

```text
Indexed 5 Golden Knowledge trios.
trace_id: f6c3e77decf84d42957992be27e2cc28
golden_knowledge_retrieved ids:
  trio_monthly_revenue_category
  trio_product_performance_returns
  trio_underperforming_branch_proxy
scores:
  0.8207
  0.6709
  0.6437
bigquery_query_succeeded: rows=10, tables=["order_items", "products"]
agent_run_completed: retrieved_trio_ids recorded
```

This verifies the full live path with Gemini chat, Gemini embeddings, Qdrant,
SQL guardrails, and BigQuery.
