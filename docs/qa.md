# Quality Assurance And Release Gates

## Reproducible Environment

```bash
uv lock --check
uv sync --frozen --all-groups
uv pip check
```

Python 3.12 and uv 0.10.8 are pinned. `pyproject.toml` is the dependency source
of truth and `uv.lock` is consumed with `--frozen` locally, in CI, and in Docker.
The runtime uses `pydantic-ai-slim[google]`; `pydantic-evals` belongs only to the
`eval` dependency group.

## Offline Test Gate

```bash
uv run ruff check .
uv run pytest \
  --cov=retail_agent \
  --cov-branch \
  --cov-report=term-missing \
  --cov-fail-under=85
```

The credential-free suite covers:

- architecture import direction and absence of evaluation imports in runtime;
- settings precedence, validation, secret masking, and prompt resources;
- conversation isolation, retention, complete multi-turn history, and tool
  summary compaction;
- model-selected retrieval/SQL paths using `TestModel` and `FunctionModel`;
- retrieval degradation, SQL retries, usage limits, structured output retries,
  evidence validation, and user-safe provider failures;
- SQL parsing, table/column scope, row projection, PII, limits, cost dry runs,
  stable job IDs, timeout, and post-submission outcome handling;
- chart tool visibility, verified-row binding, subprocess success, timeout,
  cleanup, environment minimization, source/output/capture caps, PNG/SVG
  validation, and CLI artifact rendering;
- application use cases, adapters, CLI continuity, guardrails, and quality
  scoring.

The current verified baseline is 229 tests with 89.81% branch-aware runtime
coverage, above the 85% gate.

## Evaluation Gates

Evaluation code and data live under `evals/`, outside `retail_agent/`.

### Guardrails

```bash
uv run python -m evals.run guardrails
```

The guardrail suite must pass 100%. It checks safe aggregate SQL, PII and
whole-row projection blocking, destructive SQL rejection, table scope,
excessive limits, malformed SQL feedback, output redaction, and automatic row
limits.

### Answer-quality replay

```bash
uv run python -m evals.run quality --mode replay
```

`evals/datasets/quality_eval_cases.jsonl` contains questions, optional history,
canonical SQL, semantic expectations, expected retrieval IDs, canonical and
candidate rows, reports, and analyst usefulness scores.

The evaluator scores:

- parsed SQL intent, required tables/fragments/functions, equivalent time
  intervals, and declared join keys;
- exact canonical row-set calculation accuracy, penalizing extra rows;
- retrieval Recall@3 and mean reciprocal rank;
- numeric faithfulness through the same runtime evidence policy used by the
  agent output validator;
- multi-turn intent resolution;
- analyst usefulness on a five-point scale;
- attachment of the exact verified SQL and absence of degraded/refused output.

Release thresholds are 100% safety scenarios, at least 0.95 intent and
calculation, at least 0.90 retrieval and multi-turn, MRR at least 0.80, zero
unsupported numeric claims, mean analyst usefulness at least 4/5, no score below
3/5, and no critical-case failure.

### Credentialed live quality

```bash
docker compose up -d qdrant
QDRANT_URL=http://localhost:6333 uv run python -m retail_agent index-golden --recreate
QDRANT_URL=http://localhost:6333 uv run python -m evals.run quality \
  --mode live \
  --automated-only \
  --output artifacts/quality-eval-live.json
```

Live mode executes generated and canonical BigQuery SQL against the same current
data. A transient retryable failure is retried with bounded backoff only when no
SQL tool completed, so the evaluator cannot duplicate warehouse work.

`--automated-only` is a regression gate, not release approval. The final release
rerun supplies a JSON object mapping case IDs to analyst scores from 0 through 5:

```bash
QDRANT_URL=http://localhost:6333 uv run python -m evals.run quality \
  --mode live \
  --human-scores path/to/human-scores.json \
  --output artifacts/quality-eval-live.json
```

The scheduled/manual GitHub workflow uses workload identity federation, uploads
the JSON report, and requires the analyst-scored rerun for release.

## Container Separation Gate

Build and verify both surfaces:

```bash
docker build --target runtime -t retail-agent:runtime .
docker run --rm --entrypoint python retail-agent:runtime -c \
  "import importlib.util, pathlib; \
assert importlib.util.find_spec('pydantic_evals') is None; \
assert not pathlib.Path('/app/evals').exists()"

docker build --target evaluation -t retail-agent:evaluation .
docker run --rm retail-agent:evaluation guardrails
docker run --rm retail-agent:evaluation quality --mode replay
```

The runtime image includes only runtime code, configuration, and Golden
Knowledge seed data. The evaluation target adds `pydantic-evals`, `evals/`, and
the quality dataset.

## Manual Resilience And Acceptance Checks

1. Ask a revenue question, then follow with “compare that with the prior month”
   and “plot it”; verify one conversation ID, increasing turn indexes, prior
   verified SQL context, and automatic chart creation.
2. Simulate Gemini failure before SQL; verify a typed retryable failure without
   traceback and that chat accepts the next message.
3. Simulate model failure after query execution; verify the redacted degraded
   table/SQL and no query replay.
4. Exercise SQL retry budgets 0, 1, and 2; verify the effective tool retry count.
5. Stop Qdrant; verify the retrieval tool returns degraded status and a SQL-only
   answer can still complete.
6. Attempt to call the chart tool before SQL; verify it is absent from the model
   tool catalogue.
7. Run chart code that times out, omits output, emits active SVG content, or
   exceeds configured sizes; verify typed errors and temporary cleanup.

## Reviewer Acceptance Commands

```bash
uv lock --check
uv sync --frozen --all-groups
uv pip check
uv run ruff check .
uv run pytest --cov=retail_agent --cov-branch --cov-fail-under=85
uv run python -m evals.run guardrails
uv run python -m evals.run quality --mode replay
docker build --target runtime -t retail-agent:runtime .
docker build --target evaluation -t retail-agent:evaluation .
docker run --rm retail-agent:evaluation guardrails
```
