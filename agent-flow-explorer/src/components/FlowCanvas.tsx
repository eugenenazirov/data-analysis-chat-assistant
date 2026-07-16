import { Check, CircleMinus, LoaderCircle, TriangleAlert, X } from 'lucide-react'
import type { FlowStage, StageOutcome } from '../types'

type VisualState = 'complete' | 'current' | 'pending' | StageOutcome

type FlowCanvasProps = {
  stages: FlowStage[]
  activeStageIndex: number
  selectedStageId: string
  onSelectStage: (id: string) => void
}

function stateForStage(stage: FlowStage, index: number, activeIndex: number): VisualState {
  if (index === activeIndex) return 'current'
  if (index < activeIndex) return stage.outcome ?? 'complete'
  if (stage.outcome === 'skipped' && activeIndex >= index - 1) return 'skipped'
  return 'pending'
}

function StateIcon({ state }: { state: VisualState }) {
  if (state === 'complete' || state === 'normal') return <Check size={13} />
  if (state === 'current') return <LoaderCircle size={13} />
  if (state === 'blocked' || state === 'error') return <X size={13} />
  if (state === 'degraded') return <TriangleAlert size={13} />
  if (state === 'skipped') return <CircleMinus size={13} />
  return <span className="pending-ring" />
}

export function FlowCanvas({
  stages,
  activeStageIndex,
  selectedStageId,
  onSelectStage,
}: FlowCanvasProps) {
  return (
    <section className="flow-canvas" aria-label="Request data flow">
      <div className="flow-grid">
        {stages.map((stage, index) => {
          const Icon = stage.icon
          const state = stateForStage(stage, index, activeStageIndex)
          const selected = selectedStageId === stage.id
          return (
            <button
              key={stage.id}
              type="button"
              className={`flow-node node-${index + 1} tone-${stage.tone} state-${state} ${selected ? 'is-selected' : ''}`}
              onClick={() => onSelectStage(stage.id)}
              aria-label={`${index + 1}. ${stage.title}: ${stage.subtitle}`}
              aria-current={index === activeStageIndex ? 'step' : undefined}
            >
              <span className="node-topline">
                <span className="node-number">{index + 1}</span>
                <span className="node-state" aria-hidden="true">
                  <StateIcon state={state} />
                </span>
              </span>
              <Icon className="node-icon" size={25} strokeWidth={1.65} aria-hidden="true" />
              <span className="node-label">{stage.title}</span>
              <span className="node-subtitle">{stage.subtitle}</span>
              {stage.optional ? <span className="optional-label">optional</span> : null}
            </button>
          )
        })}
      </div>
      <div className="flow-connector connector-a" aria-hidden="true" />
      <div className="flow-connector connector-b" aria-hidden="true" />
      <div className="flow-connector connector-c" aria-hidden="true" />
      <div className="flow-connector connector-turn" aria-hidden="true" />
      <div className="flow-connector connector-d" aria-hidden="true" />
      <div className="flow-connector connector-e" aria-hidden="true" />
      <div className="flow-connector connector-f" aria-hidden="true" />
    </section>
  )
}
