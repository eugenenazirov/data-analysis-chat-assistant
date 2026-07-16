import type { LucideIcon } from 'lucide-react'

export type StageTone = 'mint' | 'blue' | 'coral' | 'gold' | 'violet'
export type StageOutcome = 'normal' | 'skipped' | 'degraded' | 'blocked' | 'error'

export type CodeReference = {
  path: string
  line: number
  symbol: string
}

export type FlowStage = {
  id: string
  shortLabel: string
  title: string
  subtitle: string
  icon: LucideIcon
  tone: StageTone
  outcome?: StageOutcome
  optional?: boolean
  what: string
  why: string[]
  code: CodeReference[]
  payload: Record<string, string | number | boolean | string[]>
}

export type FlightEvent = {
  id: string
  stageId: string
  offset: string
  event: string
  source: string
  detail: string
  level: 'info' | 'ok' | 'warn' | 'error'
}

export type Scenario = {
  id: string
  title: string
  shortTitle: string
  summary: string
  question: string
  icon: LucideIcon
  tone: StageTone
  stages: FlowStage[]
  events: FlightEvent[]
  result: string
  takeaway: string
}

export type AppTab = 'route' | 'architecture' | 'guardrails' | 'telemetry'
