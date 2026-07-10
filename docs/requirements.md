# Requirements Mapping

The prototype proves the risky agent behaviors in a CLI. The production HLD in
`docs/architecture.md` specifies the durable API, storage, authorization, and
operations that are intentionally outside the assignment prototype.

## 1. Hybrid Intelligence

Prototype:

- Approved Question -> SQL -> Analyst Report trios are embedded into Qdrant.
- Retrieval occurs deterministically before every model run.
- Follow-up retrieval combines the preceding and current user questions.
- Retrieved IDs are included in structured events and `TurnResult` diagnostics.

Production:

- Interactions enter a candidate queue, never the active index directly.
- Analysts edit and approve candidates.
- PostgreSQL approval and an outbox event commit together.
- Workers write versioned raw artifacts to S3-compatible storage, build a
  versioned Qdrant collection, run release gates, and atomically promote its
  alias. The previous version remains available for rollback.

## 2. Safety And PII Masking

Prototype controls are layered:

- `sqlglot` permits only one read-only query and restricts fully qualified tables.
- Table-specific safe-column allowlists block PII, `SELECT *`, whole-row aliases,
  and hidden row serialization.
- Missing limits are added; excessive limits are rejected.
- BigQuery dry-run byte caps and timeouts bound cost.
- Recursive output and telemetry redaction masks email and phone patterns.
- The model receives explicit analytics-only and no-PII instructions.

Production adds OIDC identity, least-privilege workload credentials, row-level
security for owned data, restricted encrypted transcripts, network policies,
secret injection, and immutable audit exports. Warehouse masking remains defense
in depth rather than replacing application validation.

## 3. Destructive Saved Reports Oversight

This is production-design-only because the assignment prototype does not include
a Saved Reports library.

- Preview queries are always scoped to the authenticated owner.
- Candidate report IDs are frozen and hashed; sample titles and count are shown.
- Confirmation is one-time, owner-bound, idempotent, and expires after five
  minutes.
- The transaction locks the pending action, deletes exactly the frozen owned IDs,
  consumes the token, and appends the audit event.
- Reports created after preview are never silently included.
- Audit exports use object lock with 400-day retention.

## 4. Continuous Improvement

User level:

- The prototype reads explicit formatting preferences from configuration.
- Production persists versioned preferences in PostgreSQL through an authenticated
  self-service endpoint. The model cannot silently change them.

System level:

- Only analyst-approved interactions enter Golden Knowledge.
- Candidate index versions must pass retrieval and answer-quality gates.
- Prompt, persona, model, and index versions are recorded on every turn for
  comparison and rollback.

## 5. Resilience And Graceful Error Handling

Prototype:

- SQL validation/runtime/empty-result feedback uses a configurable 0-3 tool retry
  budget that is applied when the PydanticAI agent is constructed.
- Qdrant failure degrades retrieval without failing the answer.
- A top-level agent boundary catches provider failures and emits
  `agent_run_failed`.
- If a verified query already completed, the user receives a redacted degraded
  table and SQL. Otherwise the user receives a typed safe failure.
- `chat` continues after a failed turn; `ask` exits nonzero without a traceback.
- No whole-agent replay occurs after a tool may have executed.

Production adds per-dependency timeouts, circuit breaking, durable idempotency,
HA PostgreSQL, derived-index rebuild, bounded Kubernetes concurrency, backups,
and tested recovery objectives.

## 6. Quality Assurance

- Pytest covers deterministic safety, adapters, retries, provider failures,
  degraded reports, conversation history, contextual retrieval, CLI continuity,
  and evaluator behavior.
- Branch-aware coverage is gated at 85%.
- `eval` runs deterministic guardrail cases.
- `eval --suite quality --mode replay` scores committed answer-quality traces.
- `eval --suite quality --mode live` runs Gemini and BigQuery, compares generated
  and canonical results from the same data, and writes a versioned JSON report.
- Quality scores cover structural AST intent, calculation accuracy, Retrieval
  Recall@3 and mean reciprocal rank, lineage-aware numeric faithfulness,
  multi-turn context resolution, and analyst usefulness.
- A live release cannot pass until analyst usefulness scores are supplied.

## 7. Observability

Prototype JSONL events include trace/session/turn IDs, user, model, history size,
retrieved trio IDs, SQL validation, warehouse bytes/latency/rows, retries and
budget, failure code, degraded status, usage, redactions, and terminal status.

Production sends the same fields through OpenTelemetry to Prometheus,
Alertmanager, Grafana, Loki, and Tempo. The HLD defines concrete availability,
latency, dependency-degradation, quality, cost, PII, and ownership objectives and
alerts.

## 8. Agility And Persona Management

Prototype tone and format remain in `config/agent.yaml` for reviewer convenience.

Production separates immutable safety instructions from editable persona style.
OIDC-authorized non-developers create drafts, run validation/evals, publish a
version pointer without deployment, and roll back. Sessions and traces pin the
persona version used for reproducibility.
