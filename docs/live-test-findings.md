# Live Test Findings

## Environment

- Date: 2026-07-08
- Gcloud personal config: `<personal-gcloud-config>`
- Gcloud personal account: `<personal-account@example.com>`
- Gcloud personal project: `<your-project-id>`
- Target dataset: `bigquery-public-data.thelook_ecommerce`
- Local `.env`: configured for project `<your-project-id>`

## Findings

### 1. Personal gcloud config exists

Status: Pass

The machine has a dedicated personal gcloud configuration:

```text
<personal-gcloud-config>
  account: <personal-account@example.com>
  project: <your-project-id>
```

### 2. Active gcloud config switched to personal project

Status: Pass

The active gcloud configuration was switched from the work account to
`<personal-gcloud-config>`.

### 3. BigQuery API enablement

Status: Pass

`bigquery.googleapis.com` was enabled for `<your-project-id>`.

### 4. Application Default Credentials refresh

Status: Pass after retry with browser consent

Attempted command:

```bash
gcloud auth application-default login <personal-account@example.com> --project <your-project-id>
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
gcloud auth application-default login <personal-account@example.com> --project <your-project-id>
gcloud auth application-default set-quota-project <your-project-id>
```

Verified ADC state:

```text
type: authorized_user
quota_project_id: <your-project-id>
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
Project: <your-project-id>
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

At this point in the implementation, the local `.env` used
`EMBEDDING_PROVIDER=hash` for a no-API-key test path. The final reviewer path
now uses `EMBEDDING_PROVIDER=gemini`; see the later Gemini embedding live test.

Evidence:

```text
Indexed 5 Golden Knowledge trios.
```

### 8. Full agent path with BigQuery

Status: Pass

At this point in the implementation, the local `.env` used:

```dotenv
LLM_MODEL=google-cloud:gemini-2.5-flash
EMBEDDING_PROVIDER=hash
GOOGLE_CLOUD_LOCATION=us-central1
```

This lets the agent use Vertex Gemini through ADC instead of requiring a Google
AI Studio API key.

Commands:

```bash
gcloud services enable aiplatform.googleapis.com --project <your-project-id>
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

## Initial Bugs Found

No application bugs were found in the initial live BigQuery pass. Later
post-review hardening found and fixed deterministic guardrail and orchestration
gaps, documented below.

Notes:

- Initial ADC login failed because the OAuth browser flow did not consent to
  `cloud-platform`; retrying and granting consent resolved it.
- Full agent testing without a Google AI Studio API key is possible via
  `google-cloud:gemini-2.5-flash` after enabling Vertex AI.

## Reviewer Conditional-Pass Fixes

Status: Fixed

A strict reviewer found additional issues after the first review cycle:

- Whole-row/table-alias projections such as `SELECT u FROM users AS u` and
  `TO_JSON_STRING(u)` could bypass field-level PII checks.
- Existing excessive limits such as `LIMIT 1000000` were accepted unchanged.
- `ask`/`chat` could fail before answering when Qdrant was unavailable.
- Observability docs overstated retry and validation logging detail.
- Live-test docs contained local personal account/project identifiers.

Fixes applied:

- SQL validation now enforces table-specific safe-column allowlists, blocks
  row projections from real table aliases, blocks excessive explicit limits,
  and keeps fully qualified table scope enforcement.
- BigQuery execution logs SQL validation success/failure, cost-limit failures,
  BigQuery failure classes, retry attempt/max-retry fields, and retry feedback
  emitted to the model.
- `index-golden` still requires Qdrant, while `ask` and `chat` continue with no
  retrieved Golden Knowledge and log `golden_knowledge_unavailable`.
- Docs were updated to describe the current behavior and redact local personal
  GCP identifiers.

Regression coverage:

- `tests/test_sql_guard.py` covers row projection and excessive-limit blocking.
- `tests/test_agent.py` covers Qdrant retrieval failure fallback.
- `tests/test_bigquery.py` covers validation/cost observability events.
- `python -m retail_agent eval` includes row projection and excessive-limit
  guardrail cases.

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

## Final Reviewer-Path Verification

Status: Pass

After aligning the default config with the Vertex AI reviewer path, the full
Docker flow was rerun without command-level embedding overrides:

```bash
docker compose build app
docker compose up -d qdrant
docker compose run --rm app index-golden --recreate
docker compose run --rm app ask "Which product categories drove the most revenue last month?" --user manager_a
```

Evidence from trace `7a9a1d69d32641fd994c29f48411c891`:

```text
Indexed 5 Golden Knowledge trios.
golden_knowledge_retrieved ids:
  trio_monthly_revenue_category
  trio_product_performance_returns
  trio_underperforming_branch_proxy
scores:
  0.8207
  0.6709
  0.6437
bigquery_query_succeeded: rows=10, tables=["order_items", "products"]
agent_run_completed: retrieved_trio_ids recorded and final SQL attached
```

The CLI output included the executive report table, caveats, follow-ups, and
the executed safe SQL.

## Conditional-Pass Fix Verification

Status: Pass

After the conditional-pass fixes, the current Docker reviewer path was rerun:

```bash
docker compose build app
docker compose run --rm app eval
docker compose run --rm app bq-smoke
docker compose run --rm app index-golden --recreate
docker compose run --rm app ask "Which product categories drove the most revenue last month?" --user manager_a
docker compose down
```

Results:

- Docker build completed successfully.
- Container evals passed, including row projection and excessive-limit cases.
- BigQuery smoke test returned one aggregate row with dry-run bytes `4340064`.
- Golden Knowledge indexing reported `Indexed 5 Golden Knowledge trios.`
- Full `ask` path returned an executive table for the top 10 revenue
  categories and attached the executed safe SQL.

Evidence from trace `c230415d9642492ebbec7a45e3e52e51`:

```text
golden_knowledge_retrieved ids:
  trio_monthly_revenue_category
  trio_product_performance_returns
  trio_underperforming_branch_proxy
agent_golden_context_prepared: same ids
sql_validation_succeeded: tables=["order_items", "products"]
bigquery_query_succeeded: rows=10
agent_run_completed: retrieved_trio_ids recorded and final SQL attached
```

## Aggregate Alias Collision Fix

Status: Fixed

A follow-up review found that row-projection detection could false-block valid
aggregate aliases when the alias matched an allowed table name, for example
`COUNT(*) AS orders ORDER BY orders DESC`.

Fix applied:

- SQL validation now checks whether an unqualified column is a select-output
  alias before treating it as a whole-row table projection.
- Row projections such as `SELECT u`, `TO_JSON_STRING(u)`, and `ARRAY_AGG(u)`
  remain blocked.

Regression coverage:

- `tests/test_sql_guard.py` now covers aggregate aliases named `orders`,
  `users`, and `products`.
- Targeted row-projection tests still pass.

Historical pre-uv verification:

```text
.venv/bin/pytest -q: 39 passed
.venv/bin/python -m retail_agent eval: pass
.venv/bin/python -m pip check: no broken requirements
docker compose build app && docker compose run --rm app eval && docker compose down: pass
```

## Senior Remediation Verification

Date: 2026-07-10

The project was migrated to uv/Python 3.12 and reverified after adding
conversation history, top-level failure handling, effective runtime retry
configuration, answer-quality evaluation, and the platform-agnostic production
HLD.

Local frozen-environment verification:

```text
uv lock --check: pass
uv sync --frozen --all-groups: pass (Python 3.12.13)
uv pip check: all installed packages compatible
uv run ruff check .: pass
uv run python -m compileall -q retail_agent: pass
```

Test and evaluation verification:

```text
152 tests passed
91.12% branch-aware coverage (85% release gate)
10/10 deterministic guardrail evaluations passed
4/4 answer-quality replay cases passed
intent/calculation/Recall@3/MRR/faithfulness/multi-turn replay aggregates: 1.00
mean replay analyst usefulness: 0.90 normalized (4.5/5)
```

Container verification:

```text
docker compose build app: pass using uv.lock with --frozen --no-dev
docker compose run --rm app eval: pass
docker compose run --rm app eval --suite quality --mode replay: pass
runtime image imports retail_agent 0.1.0 and does not contain the uv executable
```

Credentialed live quality verification:

```text
Gemini Golden Knowledge indexing: 5 trios indexed
4/4 live cases completed with Gemini and BigQuery
intent aggregate: 1.00
calculation aggregate: 1.00
Retrieval Recall@3 aggregate: 1.00
Retrieval MRR aggregate: 1.00
numeric faithfulness aggregate: 1.00
multi-turn aggregate: 1.00
automated live gate: pass
```

The final report is recorded locally at `logs/quality-eval-live.json`. Its
overall `passed` field remains `false` solely because analyst usefulness scores
are deliberately unset, while `automated_passed` is `true`. The implementation
does not substitute an automated or AI-authored score for the required human
review.

The final cleanup review also verified that displayed SQL is always the exact
executed statement, chat history is byte-bounded, and live-eval backoff only
retries when the SQL tool was never invoked.

## Adversarial Evaluator And Post-Submission Retry Re-Review

Date: 2026-07-10

The follow-up review probes are now committed as regression tests:

- an additional candidate row reduces calculation accuracy instead of being ignored;
- a claim such as "Revenue was 50" cannot borrow `orders=50` or `LIMIT 50`;
- currency and percentage claims resolve only against compatible measures;
- `order_items.order_id = products.id` is rejected because join keys are declared
  explicitly in the versioned case;
- `INTERVAL 3 MONTH` and `INTERVAL 1 QUARTER` share the same normalized duration;
- additional verified measures are allowed, but the required canonical row set
  must still match exactly.

BigQuery execution now uses a stable trace/SQL-derived job ID with SDK job retry
disabled. Validation and dry-run failures may enter bounded `ModelRetry` feedback;
any failure after submission becomes non-retryable
`warehouse_outcome_unknown`, emits `sql_terminal_failure`, and cannot resubmit the
query through the model loop.

The final credentialed live trace used the strengthened follow-up cohort
instruction and passed all four automated cases at 1.00 for intent, calculation,
Recall@3, MRR, faithfulness, and multi-turn resolution; the saved trace was also
rechecked with the final percent-scaling rule. Local and containerized BigQuery
smoke tests passed with stable job IDs and no SDK retry warning.

## Contextual Number Faithfulness Re-Review

Date: 2026-07-10

The final contextual-number edge is covered by adversarial regressions:

- the current year cannot support `Revenue was 2026` or `Revenue was $2,026`;
- context language must form a number-anchored phrase such as `top 10`, `10
  results`, `last 3 months`, or `calendar year 2026`;
- currency symbols and units can never be justified by SQL context;
- current and previous years require nearby temporal language before they are
  accepted as contextual values;
- the numeral in an exact returned alphanumeric dimension such as `501 Jeans`
  is classified as dimension text;
- a bare quantitative claim of `501` cannot borrow support from that string
  dimension.

The implementation matches exact returned dimension values before evaluating
remaining numeric claims against metric columns and SQL context. Purely numeric
strings are intentionally not exempted because they are ambiguous with measures.
The saved credentialed live trace was deterministically rescored after the fix;
all four cases retained faithfulness `1.00`, including the valid `top 10
customers` framing case.

The subsequent word-order probes `Revenue this year was 2026 dollars` and
`Revenue for the top result was 10 dollars` are also full-gate regressions. Both
remain rejected without the word `dollars`, proving that phrase binding rather
than currency detection closes the bypass. Compact suffix parsing now also
distinguishes `3 months` from `3M`.

Typed-claim regressions additionally reject `top 10%` and `top 10 percent` when
the only supporting value is SQL `LIMIT 10`, and reject `2026€` when the only
support is the current-year context. Currency-code-prefixed amounts such as
`USD10M` and `USD99999999` are scanned and evaluated instead of being skipped.
Positive controls verify that `USD10M`, postfix currency symbols, and percentages
still pass when the corresponding result metric actually supports them.

Unit-safety regressions define rate columns as fractional values: `20%` matches
`return_rate=0.2`, while `0.2%` does not. Absolute counts cannot support
percentage claims, absolute differences cannot masquerade as percentage
changes, and percentage derivations contain ratios only. Currency claims fail
without a monetary result column and cannot use dimensionless ratios; monetary
sums and differences remain supported.

The live regional report using `$5,225.15`, `$2,774.17`, and the highlight
`~1.9x` is covered as a complete quality-case regression. Both `~` and `≈`
activate the same bounded approximation tolerance as words such as
`approximately`, so the computed ratio `1.8835` remains faithful when rounded to
one decimal place.

## Numeric Identifier Faithfulness Re-Review

Date: 2026-07-11

The critical live highlight `Our top spending customer (ID 67493) spent
$1549.39 across 2 orders` is covered as a complete quality-case regression.
Numeric `id` and `*_id` fields are removed from the quantitative measure pool
and validated as identifier dimensions using exact equality plus a structurally
adjacent `ID` or corresponding entity cue. A generic `ID` cue is rejected when
multiple numeric identifier columns make it ambiguous; wrong values and typed
currency/percentage identifier claims remain unsupported.
