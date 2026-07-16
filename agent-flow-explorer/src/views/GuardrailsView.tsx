import { ArrowRight, Check, Gauge, KeyRound, ShieldAlert } from 'lucide-react'
import { useState } from 'react'
import { guardrailGroups } from '../data/architecture'

const budgets = [
  ['Model requests', '8'],
  ['Tool calls', '6'],
  ['SQL repairs', '2'],
  ['Chart repairs', '2'],
  ['Output repairs', '1'],
  ['History turns', '6'],
  ['History bytes', '65,536'],
  ['Result rows', '500'],
] as const

export function GuardrailsView() {
  const [selected, setSelected] = useState('sql')
  const active = guardrailGroups.find((group) => group.id === selected)!
  const ActiveIcon = active.icon

  return (
    <main className="learning-view guardrails-view">
      <header className="view-intro">
        <div>
          <h1>Safety is a chain of independent gates</h1>
          <p>
            The model is not a security boundary. Deterministic code validates every expensive or
            sensitive transition before and after a provider call.
          </p>
        </div>
        <span className="view-path">policy → AST → dry run → evidence → redaction</span>
      </header>

      <section className="guardrail-chain">
        {guardrailGroups.map((group, index) => {
          const Icon = group.icon
          return (
            <div className="guardrail-link" key={group.id}>
              <button
                type="button"
                className={selected === group.id ? 'is-selected' : ''}
                onClick={() => setSelected(group.id)}
              >
                <small>{group.number}</small>
                <Icon size={23} />
                <strong>{group.title}</strong>
                <span>{group.summary}</span>
              </button>
              {index < guardrailGroups.length - 1 ? <ArrowRight size={19} /> : null}
            </div>
          )
        })}
      </section>

      <section className="guardrail-detail">
        <div className="guardrail-detail-main">
          <span className="guardrail-detail-icon"><ActiveIcon size={27} /></span>
          <div>
            <small>{active.number} / Guardrail group</small>
            <h2>{active.title}</h2>
            <p>{active.summary}</p>
          </div>
        </div>
        <ul>
          {active.checks.map((check) => <li key={check}><Check size={16} /> {check}</li>)}
        </ul>
        <code>{active.code}</code>
      </section>

      <section className="budget-section">
        <div className="section-heading">
          <div><Gauge size={20} /><h2>Bounded budgets from config/agent.yaml</h2></div>
          <p>Explicit limits turn an open-ended agent loop into a bounded operation.</p>
        </div>
        <div className="budget-rail">
          {budgets.map(([label, value]) => (
            <span key={label}><small>{label}</small><strong>{value}</strong></span>
          ))}
        </div>
      </section>

      <section className="boundary-comparison">
        <div>
          <ShieldAlert size={22} />
          <h3>Runtime reliability boundary</h3>
          <p>
            The local chart subprocess restricts imports, files, runtime, size, and output, but it
            is explicitly documented as <strong>not a security sandbox</strong>.
          </p>
        </div>
        <div>
          <KeyRound size={22} />
          <h3>Production security boundary</h3>
          <p>
            The HLD moves model-generated code to an isolated worker with no application
            credentials, dedicated resources, and object storage.
          </p>
        </div>
      </section>
    </main>
  )
}
