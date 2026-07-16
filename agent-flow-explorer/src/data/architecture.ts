import {
  Boxes,
  Cable,
  CircleGauge,
  Command,
  Database,
  FileCode2,
  GitMerge,
  Layers3,
  Network,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'

export const architectureLayers = [
  {
    id: 'presentation',
    order: '04',
    name: 'Presentation',
    role: 'Accepts commands and renders presentation-neutral DTOs.',
    rule: 'Contains no prompt, evaluation, or tool-ordering logic.',
    color: 'violet',
    icon: Command,
    modules: [
      'presentation/cli/app.py',
      'presentation/cli/renderer.py',
      '__main__.py',
    ],
  },
  {
    id: 'infrastructure',
    order: '03',
    name: 'Infrastructure',
    role: 'Implements outbound ports for external SDKs and runtime mechanisms.',
    rule: 'Depends on application and domain contracts; adapters are replaceable.',
    color: 'blue',
    icon: Cable,
    modules: [
      'agents/pydantic_ai_analysis_agent.py',
      'analytics/bigquery_adapter.py',
      'retrieval/qdrant_adapter.py',
      'charts/local_python_executor.py',
      'observability.py',
    ],
  },
  {
    id: 'application',
    order: '02',
    name: 'Application',
    role: 'Coordinates one turn through narrow ports and DTOs.',
    rule: 'Does not import BigQuery, Qdrant, Gemini, PydanticAI, or Typer.',
    color: 'mint',
    icon: GitMerge,
    modules: [
      'use_cases/analyze_question.py',
      'use_cases/start_conversation.py',
      'ports/agent.py',
      'ports/analytics.py',
      'dto.py',
    ],
  },
  {
    id: 'domain',
    order: '01',
    name: 'Domain',
    role: 'Pure models, errors, and deterministic policies.',
    rule: 'Knows nothing about the CLI, SDKs, settings, logging vendors, or PydanticAI.',
    color: 'gold',
    icon: ShieldCheck,
    modules: [
      'models/conversation.py',
      'models/analysis.py',
      'models/query.py',
      'policies/privacy.py',
      'policies/report_evidence.py',
    ],
  },
] as const

export const runtimeComposition = [
  { label: 'Inbound', value: 'Typer CLI', icon: Command },
  { label: 'Use case', value: 'AnalyzeQuestion', icon: Layers3 },
  { label: 'Agent port', value: 'PydanticAIAnalysisAgent', icon: Sparkles },
  { label: 'Analytics port', value: 'BigQueryAnalyticsAdapter', icon: Database },
  { label: 'Retrieval port', value: 'QdrantGoldenExampleRepository', icon: Network },
  { label: 'Chart port', value: 'LocalPythonChartExecutor', icon: FileCode2 },
] as const

export const guardrailGroups = [
  {
    id: 'routing',
    number: '01',
    title: 'Before the model',
    icon: GitMerge,
    summary: 'High-confidence refusals/clarifications + schema-only classification.',
    checks: [
      'Unsupported denominator or dimension',
      'Contradictory scope',
      'Captured UTC reference date',
      'User profile and bounded history',
    ],
    code: 'domain/policies/request_routing.py',
  },
  {
    id: 'agent',
    number: '02',
    title: 'Inside the agent',
    icon: Sparkles,
    summary: 'Dynamic tool visibility + structured output + resource budgets.',
    checks: [
      'request_limit = 8',
      'tool_calls_limit = 6',
      'total_tokens_limit = 32,000',
      'sequential FunctionToolset',
    ],
    code: 'agent.py · config/agent.yaml',
  },
  {
    id: 'sql',
    number: '03',
    title: 'SQL boundary',
    icon: ShieldCheck,
    summary: 'Question semantics + AST policy + warehouse cost boundary.',
    checks: [
      'Single read-only SELECT/UNION',
      'Table/column allowlists',
      'No SELECT *, DML/DDL, CROSS join',
      'Dry run ≤ 200 MB',
    ],
    code: 'sql_guard.py · bigquery_adapter.py',
  },
  {
    id: 'result',
    number: '04',
    title: 'After data retrieval',
    icon: CircleGauge,
    summary: 'Completeness, evidence, privacy, and traceability.',
    checks: [
      '500 rows vs available_rows',
      'Numeric claims backed by rows',
      'Artifact bound to current turn',
      'Recursive PII redaction',
    ],
    code: 'report_evidence.py · privacy.py',
  },
] as const

export const systemFacts = [
  { label: 'Core boundary', value: 'Clean Architecture', icon: Layers3 },
  { label: 'Orchestrator', value: 'PydanticAI 2.9', icon: Sparkles },
  { label: 'Warehouse', value: 'BigQuery', icon: Database },
  { label: 'Knowledge', value: 'Qdrant + Gemini embeddings', icon: Network },
  { label: 'Composition root', value: 'retail_agent/bootstrap.py', icon: Boxes },
] as const
