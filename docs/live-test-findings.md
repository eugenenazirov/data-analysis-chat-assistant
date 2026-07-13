# Verification Record

This file records reproducible project-level evidence without machine-specific
account names, project IDs, tokens, or local credential paths. Run the exact
current gates from `docs/qa.md`; credentialed results are environment-dependent
and should be stored as workflow artifacts rather than committed secrets.

## Current Offline Baseline

Verified on 2026-07-14 with Python 3.12 and uv 0.10.8:

| Gate | Result |
|---|---|
| Lockfile and environment consistency | Pass |
| Ruff | Pass |
| Pytest | 191 passed |
| Branch-aware runtime coverage | 89.01% (85% gate) |
| Guardrail evaluation | 10/10 passed |
| Answer-quality replay | 4/4 cases passed |
| Runtime image excludes `evals/` and `pydantic-evals` | Pass |
| Evaluation image guardrails and replay | Pass |

The replay aggregate met all release thresholds: SQL intent, canonical
calculation, retrieval recall, MRR, numeric faithfulness, multi-turn resolution,
critical-case pass rate, and automated pass rate were 1.0; mean analyst
usefulness was 0.9 on the normalized scale.

## Runtime And Chart Verification

Credential-free tests prove:

- model-selected retrieval and SQL paths through PydanticAI `TestModel` and
  `FunctionModel`;
- Qdrant degradation, provider failures before and after SQL, bounded output
  retries, and non-retryable unknown warehouse outcomes;
- chart-tool invisibility before verified SQL and availability afterward;
- successful PNG and SVG generation, fixed data binding, timeout and process
  cleanup, environment minimization, size limits, output validation, and CLI
  artifact rendering;
- runtime/evaluation dependency and image separation.

The Compose application mounts `/app/artifacts` in the named
`chart_artifacts` volume so generated charts survive one-shot `docker compose
run --rm` containers.

## Credentialed Live Path

The BigQuery smoke command and a four-case Gemini/BigQuery quality suite have
previously passed against `bigquery-public-data.thelook_ecommerce`. That result
is historical evidence, not a substitute for a release-time rerun because
warehouse data and model behavior can change.

Current smoke command:

```bash
docker compose run --rm app bq-smoke
```

Current live-quality procedure:

```bash
docker compose up -d qdrant
docker compose run --rm app index-golden --recreate
uv run python -m evals.run quality \
  --mode live \
  --automated-only \
  --output artifacts/quality-eval-live.json
```

For release approval, rerun with analyst scores:

```bash
uv run python -m evals.run quality \
  --mode live \
  --human-scores path/to/human-scores.json \
  --output artifacts/quality-eval-live.json
```

The scheduled/manual workflow uses workload identity federation, uploads the
JSON report, and requires the analyst-scored run for release. Local Application
Default Credentials are acceptable for an explicit developer smoke test but
must never be copied into the repository or container image.

## Interpretation

- Offline checks are deterministic release prerequisites.
- `--automated-only` live mode is a regression signal, not final approval.
- A credentialed release rerun must use current data, current model/index/prompt
  versions, and current analyst scores.
- A live dependency or credential failure must be reported as an environment
  limitation; it must not be rewritten as a passing application result.
