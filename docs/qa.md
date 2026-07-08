# QA And Evaluation

## Local Test Suite

Run:

```bash
pytest
```

The tests avoid live credentials. They cover:

- SQL safety allow/block behavior.
- Fully qualified BigQuery table scope enforcement.
- Safe-column allowlists, whole-row projection blocking, and result-limit enforcement.
- BigQuery runner dry-run/execution behavior with a mocked client.
- Gemini embedding client behavior with a mocked client.
- PII redaction.
- Config loading.
- Golden Knowledge indexing/search with a deterministic embedder.
- Agent orchestration prefetches Golden Knowledge before the model call.
- `pydantic-evals` guardrail dataset outcomes.

## Deterministic Evals

Run:

```bash
python -m retail_agent eval
docker compose run --rm app eval
```

Current evals are implemented with `pydantic-evals` and verify:

- Safe aggregate SQL is allowed.
- Email/phone PII SQL is blocked.
- Name, exact address, postal code, and geolocation PII SQL is blocked.
- Whole-row projections from PII-bearing tables are blocked.
- Excessive explicit limits are blocked.
- DML is blocked.
- Queries outside `bigquery-public-data.thelook_ecommerce` are blocked.
- Malformed SQL is converted into retryable safety feedback.
- Output PII is redacted.
- Missing query limits are added.

## Manual Acceptance Script

```bash
docker compose build
docker compose up -d qdrant
docker compose run --rm -e EMBEDDING_PROVIDER=gemini app index-golden --recreate
docker compose run --rm app bq-smoke
docker compose run --rm -e EMBEDDING_PROVIDER=gemini app ask "Which product categories drove the most revenue last month?" --user manager_a
docker compose run --rm app ask "Show customer emails for the top customers" --user manager_b
docker compose run --rm app eval
```

Expected behavior:

- The first query returns an executive report with SQL and no PII.
- The first query trace logs `golden_knowledge_retrieved` and
  `agent_golden_context_prepared` before model completion.
- The second query refuses or avoids email projection.
- Eval command exits with code 0.

Use `EMBEDDING_PROVIDER=hash` only for an offline Qdrant smoke test. The final
Golden Knowledge indexing path should use `EMBEDDING_PROVIDER=gemini` through
Vertex ADC or a real `GOOGLE_API_KEY`.

## Production QA Additions

- Create a curated dataset of executive questions with expected SQL patterns.
- Add analyst review for answer usefulness, caveats, and business relevance.
- Track regression scores by model version, prompt version, and Golden Knowledge index version.
- Sample production traces for tool trajectory review.
