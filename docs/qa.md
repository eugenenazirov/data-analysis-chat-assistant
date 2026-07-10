# Quality Assurance And Release Gates

## Frozen Environment

The project uses uv as the only Python environment and dependency manager.

```bash
uv lock --check
uv sync --frozen --all-groups
uv pip check
```

`pyproject.toml` is the dependency source of truth and `uv.lock` contains the
complete cross-platform resolution. Python 3.12 and uv 0.10.8 are pinned. CI and
Docker refuse to resolve dependencies outside the lockfile.

## Unit And Integration Tests

```bash
uv run pytest
uv run pytest \
  --cov=retail_agent \
  --cov-branch \
  --cov-report=term-missing \
  --cov-fail-under=85
```

The suite avoids live credentials and covers:

- SQL safety, PII, fully qualified table scope, row projection, and result limits.
- BigQuery validation, dry-run, cost, execution, API failure, and timeout paths.
- Gemini embeddings with mocked API and Vertex configuration.
- Golden Knowledge indexing, recreation, readiness, and retrieval.
- Runtime agent composition and effective retry budgets of 0-3.
- Qdrant degradation, provider outage, typed failures, and degraded reports.
- Conversation history, session/turn correlation, contextual follow-up retrieval,
  and history trimming.
- CLI failure continuity and failure/report rendering.
- Guardrail and answer-quality evaluator behavior.

CI additionally runs Ruff, compilation, Docker build, lockfile freshness, and a
post-sync diff check.

## Deterministic Guardrail Evaluations

```bash
uv run python -m retail_agent eval
docker compose run --rm app eval
```

The guardrail suite must pass 100%. It verifies safe SQL, PII blocking, row
projection blocking, destructive SQL rejection, table scope, excessive limits,
malformed SQL retry feedback, output redaction, and automatic limits.

## Answer-Quality Replay Evaluations

```bash
uv run python -m retail_agent eval --suite quality --mode replay
```

`data/quality_eval_cases.jsonl` is a versioned executable dataset. Each case
contains:

- a single-turn question or multi-turn history and follow-up;
- canonical SQL;
- required tables and SQL semantic fragments;
- allowed table/column join keys;
- forbidden PII fragments;
- expected Golden Knowledge IDs;
- canonical and candidate rows for deterministic replay;
- a structured report and analyst usefulness score.

The evaluator scores:

- intent-to-SQL correctness using parsed tables, required aggregate subsets,
  dimensions, filters, functions, declared join keys, normalized equivalent
  time offsets, and case-specific semantic fragments;
- calculation accuracy by exact candidate/canonical row sets. Additional rows
  reduce the score; additional columns are allowed only when every canonical
  field value still matches;
- Retrieval Recall@3 and mean reciprocal rank;
- metric-aware support for every numeric claim in query results. Currency,
  percentage, and nearby measure language select the relevant column; SQL
  context values require number-anchored phrases such as `top 10`, `10 results`,
  `last 3 months`, or `calendar year 2026`; qualifiers elsewhere in a sentence
  cannot support the number. Currency symbols and units cannot borrow context
  values.
  Numerals inside an exact returned alphanumeric dimension, such as `501 Jeans`,
  are recognized as dimension text rather than quantitative claims; derivations
  remain restricted to values from the same measure;
- multi-turn history use and structural resolution of the contextual canonical SQL;
- analyst usefulness on a five-point rubric.

Release thresholds are:

- 100% guardrail, PII, ownership, and resilience scenarios;
- at least 95% intent and calculation scores, with no critical-case failure;
- at least 90% Retrieval Recall@3 and multi-turn scores, with mean reciprocal
  rank of at least 0.8;
- zero unsupported numeric claims;
- mean analyst usefulness at least 4/5, with no case below 3/5.

## Credentialed Live Evaluation

Start Qdrant and index real Gemini embeddings:

```bash
docker compose up -d qdrant
uv run python -m retail_agent index-golden --recreate
```

Then run:

```bash
uv run python -m retail_agent eval \
  --suite quality \
  --mode live \
  --output artifacts/quality-eval-live.json
```

Live mode runs each canonical and generated query against the same current
BigQuery data, avoiding stale fixed result assertions. It records the candidate
report, retrieval, SQL, and score details. A transient retryable model failure
is retried with bounded backoff only when no SQL tool completed, so the harness
cannot duplicate warehouse work. The run remains failed with
`needs_human_review=true` until analyst scores are supplied:

```bash
uv run python -m retail_agent eval \
  --suite quality \
  --mode live \
  --automated-only \
  --output artifacts/quality-eval-live.json
```

`--automated-only` is the scheduled regression gate: it succeeds only when all
automated metrics pass while leaving `needs_human_review=true`. It is not a
release approval. The analyst-scored command remains:

```bash
uv run python -m retail_agent eval \
  --suite quality \
  --mode live \
  --human-scores path/to/human-scores.json \
  --output artifacts/quality-eval-live.json
```

The score file is a JSON object mapping case IDs to values from 0 through 5.
An LLM judge may assist review, but its score cannot replace the analyst score.

The scheduled/manual GitHub workflow authenticates with workload identity
federation, fails on automated regressions, and uploads the pending-review
report without producing a noisy failure solely because scores are absent. A
model, prompt, persona, or Golden index release requires a passing
analyst-scored rerun.

## Manual Resilience Demonstration

Before submission, demonstrate:

1. A two-turn chat such as a revenue question followed by "compare that with the
   prior month"; verify one session ID, increasing turn indexes, and non-empty
   history on the second event.
2. A simulated Gemini outage before SQL; verify a typed failure, no traceback,
   and another accepted chat message.
3. A simulated failure after query execution; verify the redacted degraded table
   and no repeated query.
4. `MAX_SQL_RETRIES=0`, `1`, and `2`; verify exact configured budgets in retry
   events.
5. Qdrant downtime; verify a successful non-retrieval answer and degradation
   metric.

## Reviewer Acceptance Commands

```bash
uv lock --check
uv sync --frozen --all-groups
uv pip check
uv run ruff check .
uv run pytest --cov=retail_agent --cov-branch --cov-fail-under=85
uv run python -m retail_agent eval
uv run python -m retail_agent eval --suite quality --mode replay
docker compose build
docker compose run --rm app eval
```
