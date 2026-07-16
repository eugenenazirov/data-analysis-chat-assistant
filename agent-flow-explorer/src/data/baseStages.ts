import {
  Bot,
  ChartNoAxesCombined,
  Database,
  FileCheck2,
  Fingerprint,
  GitBranch,
  Search,
  ShieldCheck,
} from 'lucide-react'
import type { FlowStage } from '../types'

export const baseStages: FlowStage[] = [
  {
    id: 'route',
    shortLabel: 'Route',
    title: 'Request routing',
    subtitle: 'A deterministic decision before any model call',
    icon: GitBranch,
    tone: 'mint',
    what:
      'The runtime assigns a trace ID, captures one UTC reference date, and first checks only high-confidence cases: an unsupported metric, a missing dimension, or a contradictory scope.',
    why: [
      'An obvious refusal consumes neither model tokens nor warehouse spend.',
      'Deterministic routing does not depend on how a model phrases its response.',
      'Every other question proceeds to the agent without a brittle, all-purpose classifier.',
    ],
    code: [
      { path: 'retail_agent/agent.py', line: 783, symbol: 'run_question()' },
      {
        path: 'retail_agent/domain/policies/request_routing.py',
        line: 42,
        symbol: 'classify_non_query_request()',
      },
      {
        path: 'retail_agent/domain/policies/retrieval.py',
        line: 28,
        symbol: 'is_schema_question()',
      },
    ],
    payload: {
      trace_id: '9f2e…c71a',
      reference_date: '2026-07-16',
      schema_only: false,
      chart_requested: true,
    },
  },
  {
    id: 'orchestrate',
    shortLabel: 'Agent',
    title: 'PydanticAI orchestration',
    subtitle: 'Prompt + history + a dynamic toolset',
    icon: Bot,
    tone: 'violet',
    what:
      'The agent receives its persona, a safe schema, bounded conversation history, and operating rules. The model selects only the tools it needs; PydanticAI enforces structured output, retry budgets, and usage limits.',
    why: [
      'Every response must satisfy a discriminated Pydantic contract.',
      'History is retained as complete message groups and bounded by turns and bytes.',
      'The toolset is sequential, preventing conflicting actions from running in parallel.',
    ],
    code: [
      { path: 'retail_agent/agent.py', line: 577, symbol: 'build_analysis_agent()' },
      { path: 'retail_agent/agent.py', line: 546, symbol: 'build_analysis_toolset()' },
      {
        path: 'retail_agent/infrastructure/prompts/builder.py',
        line: 49,
        symbol: 'build_analysis_prompt()',
      },
    ],
    payload: {
      model: 'google-cloud:gemini-3.5-flash',
      prompt: 'analysis-v12',
      request_limit: 8,
      tool_calls_limit: 6,
      history_turns: 6,
    },
  },
  {
    id: 'retrieve',
    shortLabel: 'Golden',
    title: 'Golden Knowledge',
    subtitle: 'An optional approved precedent from Qdrant',
    icon: Search,
    tone: 'blue',
    optional: true,
    what:
      'When a metric definition, cohort, join, or time window benefits from an approved example, the model calls retrieval with a self-contained, contextualized question. Embedding search returns the top matching Question/SQL/Report trios.',
    why: [
      'A golden example informs semantics, but its historical rows are never treated as current data.',
      'Retrieval is available at most once and disappears after SQL execution.',
      'A Qdrant failure becomes a typed degraded result and does not block BigQuery.',
    ],
    code: [
      { path: 'retail_agent/agent.py', line: 259, symbol: 'retrieve_golden_examples()' },
      {
        path: 'retail_agent/infrastructure/retrieval/qdrant_adapter.py',
        line: 96,
        symbol: 'QdrantGoldenExampleRepository.search()',
      },
    ],
    payload: {
      collection: 'golden_trios',
      top_k: 3,
      result: 'approved precedents or typed degraded',
      retry_budget: 0,
    },
  },
  {
    id: 'guard',
    shortLabel: 'Guard',
    title: 'SQL safety gates',
    subtitle: 'AST, allowlists, semantics, and cost controls',
    icon: ShieldCheck,
    tone: 'gold',
    what:
      'Generated SQL passes through two independent layers: question-specific semantic validation and a sqlglot AST guard. BigQuery then performs a dry run under a byte cap.',
    why: [
      'Only one SELECT or UNION is allowed; DDL, DML, SELECT *, and unsafe joins are rejected.',
      'Only fully qualified tables and allowlisted columns may be queried.',
      'Division requires SAFE_DIVIDE or NULLIF, and cost is checked before execution.',
    ],
    code: [
      { path: 'retail_agent/agent.py', line: 334, symbol: 'run_sql_query()' },
      { path: 'retail_agent/sql_guard.py', line: 36, symbol: 'validate_and_prepare_sql()' },
      {
        path: 'retail_agent/domain/policies/query_semantics.py',
        line: 67,
        symbol: 'validate_query_semantics()',
      },
    ],
    payload: {
      statement: 'SELECT only',
      max_bytes_billed: 200000000,
      max_rows: 500,
      sql_retries: 2,
    },
  },
  {
    id: 'warehouse',
    shortLabel: 'BigQuery',
    title: 'BigQuery execution',
    subtitle: 'Stable job ID + complete row count',
    icon: Database,
    tone: 'blue',
    what:
      'After the dry run, the query starts with a deterministic job ID. The client fetches at most 500 rows and compares that count with total_rows without injecting a misleading LIMIT into the SQL.',
    why: [
      'An unknown outcome after submission is not retried, so warehouse work cannot be duplicated.',
      'Result completeness is evaluated independently of the model preview size.',
      'The model sees at most 10 rows while the runtime retains every fetched row.',
    ],
    code: [
      {
        path: 'retail_agent/infrastructure/analytics/bigquery_adapter.py',
        line: 73,
        symbol: 'BigQueryAnalyticsAdapter.execute()',
      },
    ],
    payload: {
      client_fetch_cap: 500,
      model_preview: 10,
      sql_limit_injected: false,
      timeout_seconds: 60,
    },
  },
  {
    id: 'chart',
    shortLabel: 'Chart',
    title: 'Chart generation',
    subtitle: 'Only after a complete, verified result',
    icon: ChartNoAxesCombined,
    tone: 'violet',
    optional: true,
    what:
      'After a successful, complete SQL result, generate_chart becomes available. The model writes a short Python program that reads a redacted input.json and produces exactly chart.png, or chart.svg when explicitly requested.',
    why: [
      'The chart tool is physically unavailable until SQL has been verified.',
      'Retries repair chart code without repeating the warehouse query.',
      'AST, imports, timeout, output size, and SVG content are all validated.',
    ],
    code: [
      { path: 'retail_agent/agent.py', line: 422, symbol: 'prepare_chart_tool()' },
      { path: 'retail_agent/agent.py', line: 462, symbol: 'generate_chart()' },
      {
        path: 'retail_agent/infrastructure/charts/local_python_executor.py',
        line: 127,
        symbol: 'LocalPythonChartExecutor.execute()',
      },
    ],
    payload: {
      input: 'recursively redacted verified rows',
      output: 'chart.png | chart.svg',
      timeout_seconds: 10,
      total_attempts: 3,
    },
  },
  {
    id: 'evidence',
    shortLabel: 'Evidence',
    title: 'Evidence validation',
    subtitle: 'Numeric claims and artifact binding',
    icon: FileCheck2,
    tone: 'mint',
    what:
      'The Pydantic output validator requires an executed query for any data answer, verifies numeric claims against trusted rows, rejects row-dump narratives, and accepts only a chart artifact created in the current turn.',
    why: [
      'SQL in the final DTO comes from the tool result, never from model prose.',
      'Unsupported numeric claims are replaced with exact verified values.',
      'An empty result must be stated plainly, and any hallucinated chart is removed.',
    ],
    code: [
      { path: 'retail_agent/agent.py', line: 594, symbol: 'validate_output()' },
      {
        path: 'retail_agent/domain/policies/report_evidence.py',
        line: 59,
        symbol: 'assess_report_evidence()',
      },
      {
        path: 'retail_agent/domain/policies/analysis_output.py',
        line: 20,
        symbol: 'narrative_output_violation()',
      },
    ],
    payload: {
      output_contract: 'AnalysisResult',
      highlights_max: 2,
      output_retries: 1,
      sql_source: 'executed QueryResult',
    },
  },
  {
    id: 'finalize',
    shortLabel: 'Finalize',
    title: 'Redact, persist, observe',
    subtitle: 'Safe DTO + bounded history + JSONL',
    icon: Fingerprint,
    tone: 'mint',
    what:
      'The runtime attaches only executed SQL and a real chart, recursively redacts PII, stores the completed turn pair in an in-memory conversation repository, and writes structured JSONL events.',
    why: [
      'Redaction covers the response, tool summaries, and telemetry.',
      'History retains bounded, verified tool context rather than an uncontrolled transcript.',
      'trace_id links the response, BigQuery job, tool events, and failure diagnostics.',
    ],
    code: [
      {
        path: 'retail_agent/application/use_cases/analyze_question.py',
        line: 24,
        symbol: 'AnalyzeQuestion.execute()',
      },
      { path: 'retail_agent/domain/policies/privacy.py', line: 33, symbol: 'redact_value()' },
      {
        path: 'retail_agent/infrastructure/observability.py',
        line: 21,
        symbol: 'EventLogger.event()',
      },
    ],
    payload: {
      response: 'presentation-neutral DTO',
      retained_turns: 20,
      trace: 'logs/agent-runs.jsonl',
      pii_redaction: 'recursive',
    },
  },
]

export function stageById(id: string): FlowStage {
  const stage = baseStages.find((item) => item.id === id)
  if (!stage) throw new Error(`Unknown stage: ${id}`)
  return stage
}

export function stageVariant(
  id: string,
  overrides: Partial<FlowStage> = {},
): FlowStage {
  return { ...stageById(id), ...overrides }
}
