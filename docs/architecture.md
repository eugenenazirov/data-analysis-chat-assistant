# Architecture

## High-Level Design

```mermaid
flowchart TD
    Manager["Store / Regional Manager CLI"] --> App["Retail Agent App"]
    App --> Config["Persona + Safety Config"]
    App --> Retriever["Golden Knowledge Retriever"]
    Retriever --> Qdrant["Qdrant Vector Store"]
    Retriever --> GoldenRaw["Golden Bucket Raw Trios"]
    Retriever --> Agent["PydanticAI Analysis Agent"]
    Agent --> Guard["SQL Guardrails"]
    Guard --> BQ["BigQuery thelook_ecommerce"]
    BQ --> Guard
    Guard --> Agent
    Agent --> Redactor["PII Output Redactor"]
    Redactor --> Manager
    App --> Logs["JSONL Logs / OTLP / Logfire"]
```

## Request Flow

```mermaid
sequenceDiagram
    participant U as Manager
    participant CLI as CLI App
    participant A as PydanticAI Agent
    participant Q as Qdrant
    participant G as SQL Guard
    participant B as BigQuery
    participant L as Logs

    U->>CLI: ask question
    CLI->>L: run_started
    CLI->>Q: retrieve similar Golden Trios
    Q-->>CLI: analyst precedent ids + content
    CLI->>A: question + user profile + schema context + precedent
    A->>G: proposed SQL
    G->>G: SELECT-only, table allowlist, PII block, limit
    G->>B: dry-run with byte cap
    B-->>G: bytes estimate
    G->>B: execute read-only query
    B-->>G: rows
    G-->>A: QueryResult
    A-->>CLI: structured AnalysisReport
    CLI->>CLI: redact final text
    CLI->>L: run_completed
    CLI-->>U: executive report
```

## Core Components

- **CLI app**: Typer/Rich interface for `chat`, `ask`, `index-golden`, and `eval`.
- **PydanticAI agent**: Typed dependencies and structured `AnalysisReport` output.
- **Golden Knowledge**: Raw JSONL seed data plus Qdrant vector index. Production stores raw trios in object storage or a database and indexes them in Qdrant Cloud/self-hosted.
- **SQL guardrails**: `sqlglot` parses generated SQL before BigQuery sees it.
- **BigQuery runner**: Runs dry-run cost checks, then read-only query jobs with byte caps, timeouts, labels, and structured errors.
- **Observability**: Local JSONL logs by default; Logfire/OpenTelemetry can be enabled without changing application code.

## Production Notes

- The same app image can run on Cloud Run, ECS, Kubernetes, or another container runtime.
- Qdrant can be local for Compose, self-hosted for private infrastructure, or Qdrant Cloud.
- BigQuery is the reference warehouse because the assignment dataset is there; the app boundaries allow another warehouse runner to replace it.
- Secrets belong in managed secret storage in production. `.env` is only for local/demo use.
