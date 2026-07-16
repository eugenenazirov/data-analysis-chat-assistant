import { Activity, Braces, CircleDot, Filter, ScanSearch } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { Scenario } from '../types'

type TelemetryViewProps = {
  scenario: Scenario
  onScenarioChange: (id: string) => void
  scenarios: Scenario[]
}

export function TelemetryView({ scenario, onScenarioChange, scenarios }: TelemetryViewProps) {
  const [level, setLevel] = useState<'all' | 'ok' | 'warn' | 'error'>('all')
  const filtered = useMemo(
    () => scenario.events.filter((item) => level === 'all' || item.level === level),
    [level, scenario.events],
  )

  const finalEvent = scenario.events.at(-1)
  const toolSequence = scenario.events.flatMap((item) => {
    if (item.stageId === 'retrieve') return ['retrieve_golden_examples']
    if (item.stageId === 'warehouse') return ['run_sql_query']
    if (item.stageId === 'chart') return ['generate_chart']
    return []
  })
  const tracePayload = {
    trace_id: '9f2e3a8d01b84a7bbfc93c42b1d0c71a',
    session_id: 'reviewer-demo',
    scenario: scenario.id,
    tool_sequence: toolSequence,
    terminal_event: finalEvent?.event,
    result: scenario.result,
  }

  return (
    <main className="learning-view telemetry-view">
      <header className="view-intro telemetry-intro">
        <div>
          <h1>One trace connects the full request lifecycle</h1>
          <p>
            After recursive redaction, JSONL telemetry records versions, tool order, retries,
            latency, usage, cost evidence, degradation, and artifacts.
          </p>
        </div>
        <label className="scenario-select">
          <span>Scenario</span>
          <select value={scenario.id} onChange={(event) => onScenarioChange(event.target.value)}>
            {scenarios.map((item) => <option key={item.id} value={item.id}>{item.shortTitle}</option>)}
          </select>
        </label>
      </header>

      <section className="trace-workspace">
        <div className="trace-events">
          <div className="trace-toolbar">
            <span><Activity size={17} /> agent-runs.jsonl</span>
            <div className="trace-filters" role="group" aria-label="Event severity filter">
              <Filter size={14} />
              {(['all', 'ok', 'warn', 'error'] as const).map((value) => (
                <button
                  type="button"
                  key={value}
                  className={level === value ? 'is-active' : ''}
                  onClick={() => setLevel(value)}
                >
                  {value}
                </button>
              ))}
            </div>
          </div>
          <div className="trace-timeline">
            {filtered.map((item, index) => (
              <div className={`trace-event level-${item.level}`} key={item.id}>
                <span className="trace-line" aria-hidden="true" />
                <span className="trace-dot"><CircleDot size={15} /></span>
                <span className="trace-index">{String(index + 1).padStart(2, '0')}</span>
                <div>
                  <span className="trace-meta"><code>{item.offset}</code><small>{item.source}</small></span>
                  <strong>{item.event}</strong>
                  <p>{item.detail}</p>
                </div>
              </div>
            ))}
          </div>
        </div>

        <aside className="trace-payload">
          <div className="payload-title"><Braces size={18} /><span>Linked trace</span></div>
          <pre>{JSON.stringify(tracePayload, null, 2)}</pre>
          <div className="trace-reading-guide">
            <h3><ScanSearch size={17} /> What reviewers should verify</h3>
            <ul>
              <li>Does <code>tool_sequence</code> match the permitted order?</li>
              <li>Was BigQuery executed again after a successful query?</li>
              <li>Is a retrieval outage explicitly marked as degraded?</li>
              <li>Is the artifact bound to the current trace?</li>
              <li>How many output and tool retries were actually consumed?</li>
            </ul>
          </div>
        </aside>
      </section>
    </main>
  )
}
