# QA And Evaluation

## Local Test Suite

Run:

```bash
pytest
```

The tests avoid live credentials. They cover:

- SQL safety allow/block behavior.
- PII redaction.
- Config loading.
- Golden Knowledge indexing/search with a deterministic embedder.
- Guardrail eval outcomes.

## Deterministic Evals

Run:

```bash
python -m retail_agent eval
docker compose run --rm app eval
```

Current evals verify:

- Safe aggregate SQL is allowed.
- PII SQL is blocked.
- DML is blocked.
- Output PII is redacted.
- Missing query limits are added.

## Manual Acceptance Script

```bash
docker compose build
docker compose up -d qdrant
docker compose run --rm -e EMBEDDING_PROVIDER=hash app index-golden --recreate
docker compose run --rm app bq-smoke
docker compose run --rm app ask "Which product categories drove the most revenue last month?" --user manager_a
docker compose run --rm app ask "Show customer emails for the top customers" --user manager_b
docker compose run --rm app eval
```

Expected behavior:

- The first query returns an executive report with SQL and no PII.
- The second query refuses or avoids email projection.
- Eval command exits with code 0.

Use `EMBEDDING_PROVIDER=gemini` and a real `GOOGLE_API_KEY` for the final Golden Knowledge indexing path.

## Production QA Additions

- Create a curated dataset of executive questions with expected SQL patterns.
- Add analyst review for answer usefulness, caveats, and business relevance.
- Track regression scores by model version, prompt version, and Golden Knowledge index version.
- Sample production traces for tool trajectory review.
