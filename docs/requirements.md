# Requirements Mapping

## 1. Hybrid Intelligence

The prototype indexes analyst-approved Question → SQL → Report trios into Qdrant. At query time, the agent retrieves similar trios and uses them as precedent for business logic, not as fresh data.

Production loop:

1. Analyst approves or edits a generated report.
2. The raw trio is stored with metadata, reviewer, timestamp, and data-domain tags.
3. A background indexer embeds the trio and writes it to Qdrant.
4. Retrieval logs record which trios influenced each answer.

## 2. Safety And PII Masking

Controls are layered:

- SQL parser blocks DML/DDL.
- SQL parser restricts tables to assignment tables.
- SQL parser blocks configured PII columns such as `email` and `phone`.
- The app redacts email/phone patterns from final outputs and logs.
- BigQuery service account should be read-only.
- Production can add BigQuery column-level security and data masking policies.

## 3. Destructive Saved Reports Oversight

The prototype does not implement destructive saved-report deletion. The production design treats it as a separate workflow:

1. User asks to delete reports.
2. Report service verifies ownership and scopes candidates.
3. Assistant shows a preview count and sample report titles.
4. User must confirm with a generated confirmation token.
5. Service deletes only owned reports and writes an audit event.
6. Ambiguous or broad requests require narrowing before confirmation.

## 4. Continuous Improvement

User level:

- User profiles in `config/agent.yaml` define preferred report format and tone.
- Production should store these preferences in a user profile database.

System level:

- Successful interactions are candidates for review.
- Only human-approved interactions enter Golden Knowledge.
- Eval failures and low-confidence traces feed backlog items.

## 5. Resilience And Graceful Error Handling

- BigQuery dry-runs catch syntax and cost issues before execution.
- Query execution is capped with timeout and `maximum_bytes_billed`.
- PydanticAI `ModelRetry` gives the model structured feedback for repair.
- Empty result sets trigger one refinement attempt, then a clear explanation.
- Qdrant readiness is checked at startup.
- Logs preserve error class, trace ID, SQL validation decision, and retry count.

## 6. Quality Assurance

- Pytest covers deterministic guardrails, redaction, config, and Qdrant indexing with mocked services.
- `python -m retail_agent eval` runs guardrail evals without live BigQuery/Gemini credentials.
- Production evaluation should add analyst-labeled cases, trajectory review, and LLM-as-judge rubrics for intent coverage.

## 7. Observability

Each run emits JSONL events with:

- `trace_id`
- user id
- retrieved Golden Trio ids
- SQL validation status
- dry-run bytes
- BigQuery latency and row count
- retry/failure class
- final refusal status
- redaction count

Logfire/OpenTelemetry can export traces to an external backend.

## 8. Persona Management

Prototype tone and format live in `config/agent.yaml`. Production should move these instructions into an admin-editable config store with validation, versioning, approvals, and rollback.
