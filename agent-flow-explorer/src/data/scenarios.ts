import {
  ChartSpline,
  DatabaseZap,
  FileQuestion,
  LockKeyhole,
  SearchX,
  ShieldX,
  Table2,
  Wrench,
} from 'lucide-react'
import type { FlightEvent, Scenario } from '../types'
import { stageVariant } from './baseStages'

const event = (
  id: string,
  stageId: string,
  offset: string,
  source: string,
  detail: string,
  level: FlightEvent['level'] = 'info',
): FlightEvent => ({ id, stageId, offset, event: id, source, detail, level })

export const scenarios: Scenario[] = [
  {
    id: 'analysis-chart',
    title: 'Analytics request with a chart',
    shortTitle: 'Analytics + chart',
    summary: 'The full success path through all three tools.',
    question:
      'Show revenue by category for the past six months and create a chart.',
    icon: ChartSpline,
    tone: 'mint',
    stages: [
      stageVariant('route'),
      stageVariant('orchestrate'),
      stageVariant('retrieve'),
      stageVariant('guard'),
      stageVariant('warehouse'),
      stageVariant('chart'),
      stageVariant('evidence'),
      stageVariant('finalize'),
    ],
    events: [
      event('agent_run_started', 'route', '+000 ms', 'run_question', 'schema_only=false · chart_requested=true'),
      event('model_request', 'orchestrate', '+142 ms', 'PydanticAI', 'history=2 messages · toolset=retrieve/sql'),
      event('golden_knowledge_retrieved', 'retrieve', '+486 ms', 'Qdrant', 'top_k=3 · ids=[monthly-revenue, category-sales]', 'ok'),
      event('sql_validation_succeeded', 'guard', '+811 ms', 'sqlglot', 'SELECT · allowlisted tables · safe columns', 'ok'),
      event('bigquery_query_succeeded', 'warehouse', '+1.84 s', 'BigQuery', 'rows=24 · available_rows=24 · truncated=false', 'ok'),
      event('chart_execution_completed', 'chart', '+2.31 s', 'chart worker', 'chart.png · 94,612 bytes · input_redactions=0', 'ok'),
      event('output_validated', 'evidence', '+2.48 s', 'validator', 'numeric claims supported · chart bound', 'ok'),
      event('agent_run_completed', 'finalize', '+2.51 s', 'telemetry', 'tool_order_compliant=true · degraded=false', 'ok'),
    ],
    result: 'Verified report + 24 rows + executed SQL + chart.png + trace_id',
    takeaway:
      'The longest path illustrates the core design: the model plans, while the runtime exposes capabilities in sequence and grounds the final response in verified data.',
  },
  {
    id: 'simple-query',
    title: 'Simple query without retrieval or a chart',
    shortTitle: 'Simple SQL',
    summary: 'The model skips tools that add no value.',
    question: 'How many orders did we receive yesterday?',
    icon: Table2,
    tone: 'blue',
    stages: [
      stageVariant('route'),
      stageVariant('orchestrate'),
      stageVariant('retrieve', {
        outcome: 'skipped',
        subtitle: 'Skipped: a precedent would not improve a simple COUNT',
      }),
      stageVariant('guard'),
      stageVariant('warehouse'),
      stageVariant('chart', {
        outcome: 'skipped',
        subtitle: 'Skipped: the user did not request a visualization',
      }),
      stageVariant('evidence'),
      stageVariant('finalize'),
    ],
    events: [
      event('agent_run_started', 'route', '+000 ms', 'run_question', 'reference_date captured once'),
      event('model_request', 'orchestrate', '+118 ms', 'PydanticAI', 'model chooses direct SQL'),
      event('sql_validation_succeeded', 'guard', '+392 ms', 'sqlglot', 'single aggregate SELECT', 'ok'),
      event('bigquery_query_succeeded', 'warehouse', '+921 ms', 'BigQuery', 'rows=1 · complete', 'ok'),
      event('output_validated', 'evidence', '+1.04 s', 'validator', 'COUNT claim matches verified row', 'ok'),
      event('agent_run_completed', 'finalize', '+1.06 s', 'telemetry', 'tool_sequence=[run_sql_query]', 'ok'),
    ],
    result: 'Concise verified answer + 1 row + executed SQL + trace_id',
    takeaway:
      'Tool use is not ceremonial: retrieval and chart generation are hidden or skipped whenever they would not improve the answer.',
  },
  {
    id: 'schema-only',
    title: 'Schema-only explanation',
    shortTitle: 'Schema-only',
    summary: 'Structured model output with data tools hidden.',
    question: 'Which tables and safe fields are available?',
    icon: FileQuestion,
    tone: 'violet',
    stages: [
      stageVariant('route', {
        payload: {
          trace_id: 'a3b1…be09',
          reference_date: '2026-07-16',
          schema_only: true,
          chart_requested: false,
        },
      }),
      stageVariant('orchestrate', {
        subtitle: 'Structured SchemaExplanationResult',
      }),
      stageVariant('retrieve', {
        outcome: 'skipped',
        subtitle: 'Hidden by the tool preparation function',
      }),
      stageVariant('guard', {
        outcome: 'skipped',
        subtitle: 'SQL tool hidden by the preparation function',
      }),
      stageVariant('warehouse', {
        outcome: 'skipped',
        subtitle: 'The warehouse is never called',
      }),
      stageVariant('chart', {
        outcome: 'skipped',
        subtitle: 'Charting is unavailable without a verified query',
      }),
      stageVariant('evidence', {
        subtitle: 'The structured output type is validated',
      }),
      stageVariant('finalize'),
    ],
    events: [
      event('agent_run_started', 'route', '+000 ms', 'router', 'schema_only=true'),
      event('tools_prepared', 'orchestrate', '+097 ms', 'PydanticAI', 'retrieval/sql/chart hidden'),
      event('schema_output_received', 'evidence', '+612 ms', 'validator', 'SchemaExplanationResult'),
      event('agent_run_completed', 'finalize', '+626 ms', 'telemetry', 'tool_sequence=[]', 'ok'),
    ],
    result: 'SchemaExplanationResult with no retrieval, SQL, or BigQuery cost',
    takeaway:
      'Schema-only is not a hard-coded response: the model explains a dynamically loaded, allowlisted schema while every data-reading tool remains physically hidden.',
  },
  {
    id: 'deterministic-refusal',
    title: 'Deterministic refusal before the model',
    shortTitle: 'Pre-model refusal',
    summary: 'An impossible metric stops before any LLM or SQL call.',
    question: 'Calculate the visitor-to-order conversion rate by branch.',
    icon: ShieldX,
    tone: 'coral',
    stages: [
      stageVariant('route', {
        outcome: 'blocked',
        title: 'Deterministic refusal',
        subtitle: 'No visit/session denominator or branch dimension',
        payload: {
          disposition: 'refuse',
          provider_requests: 0,
          warehouse_queries: 0,
          refused: true,
        },
      }),
      stageVariant('orchestrate', { outcome: 'skipped' }),
      stageVariant('retrieve', { outcome: 'skipped' }),
      stageVariant('guard', { outcome: 'skipped' }),
      stageVariant('warehouse', { outcome: 'skipped' }),
      stageVariant('chart', { outcome: 'skipped' }),
      stageVariant('evidence', { outcome: 'skipped' }),
      stageVariant('finalize', {
        subtitle: 'The refusal is logged and the turn boundary is preserved',
      }),
    ],
    events: [
      event('agent_run_started', 'route', '+000 ms', 'router', 'deterministic_disposition=refuse'),
      event('agent_run_completed', 'finalize', '+002 ms', 'telemetry', 'provider_requests=0 · sql=null · refused=true', 'ok'),
    ],
    result: 'Explainable refusal + safe alternative + trace_id',
    takeaway:
      'High-confidence limitations are encoded as domain policy, giving the system predictable behavior with zero external-call cost.',
  },
  {
    id: 'pii-refusal',
    title: 'PII request: the model calls no tools',
    shortTitle: 'PII request',
    summary: 'Policy-guided refusal + recursive redaction.',
    question: 'Show the email address and phone number of the top 10 customers.',
    icon: LockKeyhole,
    tone: 'coral',
    stages: [
      stageVariant('route'),
      stageVariant('orchestrate', {
        outcome: 'blocked',
        subtitle: 'Prompt policy requires UnsupportedRequest',
      }),
      stageVariant('retrieve', { outcome: 'skipped' }),
      stageVariant('guard', { outcome: 'skipped' }),
      stageVariant('warehouse', { outcome: 'skipped' }),
      stageVariant('chart', { outcome: 'skipped' }),
      stageVariant('evidence', {
        subtitle: 'Structured refusal with no data result',
      }),
      stageVariant('finalize', {
        subtitle: 'Recursive defense-in-depth redaction',
      }),
    ],
    events: [
      event('agent_run_started', 'route', '+000 ms', 'router', 'schema_only=false'),
      event('unsupported_request', 'orchestrate', '+421 ms', 'PydanticAI', 'model selected no tools', 'warn'),
      event('output_validated', 'evidence', '+438 ms', 'validator', 'UnsupportedRequest contract'),
      event('agent_run_completed', 'finalize', '+451 ms', 'telemetry', 'sql=null · tool_sequence=[] · redacted', 'ok'),
    ],
    result: 'Refusal to expose direct identifiers + a safe aggregation alternative',
    takeaway:
      'PII is protected by more than a single regex: safe-column allowlists block SQL, the prompt guides safe behavior, and recursive redaction protects every outbound surface.',
  },
  {
    id: 'retrieval-degraded',
    title: 'Qdrant is unavailable; SQL continues',
    shortTitle: 'Retrieval degraded',
    summary: 'Typed degradation instead of a false “nothing found.”',
    question: 'Compare returns by category for the quarter.',
    icon: SearchX,
    tone: 'gold',
    stages: [
      stageVariant('route'),
      stageVariant('orchestrate'),
      stageVariant('retrieve', {
        outcome: 'degraded',
        subtitle: 'Qdrant outage → typed degraded result',
        payload: {
          status: 'degraded',
          error_code: 'retrieval_unavailable',
          blocks_sql: false,
          retries: 0,
        },
      }),
      stageVariant('guard'),
      stageVariant('warehouse'),
      stageVariant('chart', { outcome: 'skipped' }),
      stageVariant('evidence'),
      stageVariant('finalize', {
        subtitle: 'The report carries degraded=true and an explicit caveat',
      }),
    ],
    events: [
      event('agent_run_started', 'route', '+000 ms', 'run_question', 'retrieval_strategy=model_selected'),
      event('model_request', 'orchestrate', '+131 ms', 'PydanticAI', 'retrieval selected'),
      event('golden_knowledge_unavailable', 'retrieve', '+388 ms', 'Qdrant', 'RetrievalError → status=degraded', 'warn'),
      event('sql_validation_succeeded', 'guard', '+672 ms', 'sqlglot', 'safe SQL', 'ok'),
      event('bigquery_query_succeeded', 'warehouse', '+1.31 s', 'BigQuery', 'verified rows complete', 'ok'),
      event('output_validated', 'evidence', '+1.47 s', 'validator', 'claims supported', 'ok'),
      event('agent_run_completed', 'finalize', '+1.49 s', 'telemetry', 'degraded=true · retrieval_degraded=true', 'warn'),
    ],
    result: 'Verified warehouse report with an explicit Golden Knowledge outage caveat',
    takeaway:
      'Retrieval improves definitions without becoming a single point of failure. The user sees an honest degraded status while the warehouse data remains fully verified.',
  },
  {
    id: 'truncated-result',
    title: 'The result exceeds 500 rows',
    shortTitle: '>500 rows',
    summary: 'No conclusions or chart from an incomplete result set.',
    question: 'Show every sale by SKU for the past year and create a heatmap.',
    icon: DatabaseZap,
    tone: 'gold',
    stages: [
      stageVariant('route'),
      stageVariant('orchestrate'),
      stageVariant('retrieve'),
      stageVariant('guard'),
      stageVariant('warehouse', {
        outcome: 'degraded',
        subtitle: '500 returned out of 12,842 available rows',
        payload: {
          returned_rows: 500,
          available_rows: 12842,
          truncated: true,
          sql_limit_injected: false,
        },
      }),
      stageVariant('chart', {
        outcome: 'skipped',
        subtitle: 'Hidden: an incomplete result must not be visualized',
      }),
      stageVariant('evidence', {
        subtitle: 'Deterministic 20-row preview + a request to narrow scope',
      }),
      stageVariant('finalize'),
    ],
    events: [
      event('agent_run_started', 'route', '+000 ms', 'run_question', 'chart_requested=true'),
      event('model_request', 'orchestrate', '+141 ms', 'PydanticAI', 'retrieval → SQL'),
      event('golden_knowledge_retrieved', 'retrieve', '+399 ms', 'Qdrant', 'top_k=3', 'ok'),
      event('sql_validation_succeeded', 'guard', '+691 ms', 'sqlglot', 'query safe', 'ok'),
      event('bigquery_query_succeeded', 'warehouse', '+2.18 s', 'BigQuery', 'rows=500 · available_rows=12842 · truncated=true', 'warn'),
      event('truncated_report_built', 'evidence', '+2.19 s', 'runtime', '20-row preview · ask to narrow scope', 'warn'),
      event('agent_run_completed', 'finalize', '+2.21 s', 'telemetry', 'chart_artifact=null', 'ok'),
    ],
    result: 'Exact counts + 20-row preview + a narrower-scope prompt; no chart or interpretation',
    takeaway:
      '500 is a client fetch cap, not a SQL LIMIT. That distinction is critical: the agent knows the true result size and avoids confident conclusions from truncated data.',
  },
  {
    id: 'chart-repair',
    title: 'A chart failure is repaired without rerunning SQL',
    shortTitle: 'Chart repair',
    summary: 'Up to three chart attempts; one warehouse execution.',
    question: 'Create an SVG chart of weekly order volume.',
    icon: Wrench,
    tone: 'violet',
    stages: [
      stageVariant('route'),
      stageVariant('orchestrate'),
      stageVariant('retrieve', { outcome: 'skipped' }),
      stageVariant('guard'),
      stageVariant('warehouse'),
      stageVariant('chart', {
        outcome: 'degraded',
        subtitle: 'attempt 1 fails → bounded repair → attempt 2 succeeds',
        payload: {
          first_error: 'output_missing',
          repair_hint: 'Save exactly chart.svg',
          second_attempt: 'succeeded',
          repeated_sql: false,
        },
      }),
      stageVariant('evidence'),
      stageVariant('finalize'),
    ],
    events: [
      event('agent_run_started', 'route', '+000 ms', 'run_question', 'chart_requested=true'),
      event('model_request', 'orchestrate', '+129 ms', 'PydanticAI', 'SQL selected directly'),
      event('sql_validation_succeeded', 'guard', '+521 ms', 'sqlglot', 'safe weekly aggregate', 'ok'),
      event('bigquery_query_succeeded', 'warehouse', '+1.12 s', 'BigQuery', 'execution #1 · rows=26 · complete', 'ok'),
      event('chart_execution_failed', 'chart', '+1.41 s', 'chart worker', 'output_missing · ModelRetry', 'error'),
      event('chart_execution_completed', 'chart', '+1.79 s', 'chart worker', 'attempt=2 · chart.svg · SQL not repeated', 'ok'),
      event('output_validated', 'evidence', '+1.92 s', 'validator', 'artifact path matches tool result', 'ok'),
      event('agent_run_completed', 'finalize', '+1.95 s', 'telemetry', 'bigquery_executions=1 · chart attempts=2', 'ok'),
    ],
    result: 'Verified report + repaired chart.svg; duplicate_warehouse_executions=0',
    takeaway:
      'Retry budgets are scoped to their boundary: chart code may be repaired, but an already successful, expensive SQL query is never executed again.',
  },
]

export const scenarioMap = new Map(scenarios.map((scenario) => [scenario.id, scenario]))
