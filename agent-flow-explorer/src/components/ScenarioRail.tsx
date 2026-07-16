import { ArrowRight, CircleHelp, Play } from 'lucide-react'
import type { Scenario } from '../types'

type ScenarioRailProps = {
  scenarios: Scenario[]
  selectedId: string
  onSelect: (id: string) => void
}

export function ScenarioRail({ scenarios, selectedId, onSelect }: ScenarioRailProps) {
  return (
    <aside className="scenario-rail" aria-label="Request scenarios">
      <div className="rail-heading">
        <span>Scenarios</span>
        <CircleHelp size={16} strokeWidth={1.8} aria-hidden="true" />
      </div>
      <div className="scenario-list">
        {scenarios.map((scenario) => {
          const Icon = scenario.icon
          const selected = scenario.id === selectedId
          return (
            <button
              type="button"
              key={scenario.id}
              className={`scenario-item tone-${scenario.tone} ${selected ? 'is-selected' : ''}`}
              onClick={() => onSelect(scenario.id)}
              aria-pressed={selected}
            >
              <span className="scenario-icon" aria-hidden="true">
                <Icon size={22} strokeWidth={1.7} />
              </span>
              <span className="scenario-copy">
                <strong>{scenario.shortTitle}</strong>
                <small>{scenario.summary}</small>
              </span>
              {selected ? (
                <Play className="scenario-arrow" size={15} fill="currentColor" />
              ) : (
                <ArrowRight className="scenario-arrow" size={15} />
              )}
            </button>
          )
        })}
      </div>

      <div className="status-legend">
        <span className="legend-title">State legend</span>
        <span><i className="legend-dot complete" />Completed</span>
        <span><i className="legend-dot current" />Current step</span>
        <span><i className="legend-dot pending" />Pending</span>
        <span><i className="legend-dot degraded" />Degraded</span>
        <span><i className="legend-dot blocked" />Blocked / refused</span>
        <span><i className="legend-dot skipped" />Unavailable / skipped</span>
      </div>
    </aside>
  )
}
