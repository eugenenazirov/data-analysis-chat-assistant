import { Braces, Check, Code2, Copy, ExternalLink } from 'lucide-react'
import { useState } from 'react'
import type { FlowStage } from '../types'

type InspectorProps = {
  stage: FlowStage
  activeStep: number
  totalSteps: number
}

export function Inspector({ stage, activeStep, totalSteps }: InspectorProps) {
  const Icon = stage.icon
  const [copied, setCopied] = useState(false)
  const payload = JSON.stringify(stage.payload, null, 2)

  const copyPayload = async () => {
    await navigator.clipboard.writeText(payload)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1400)
  }

  return (
    <aside className={`inspector tone-${stage.tone}`} aria-label="Selected step details">
      <div className="inspector-step">Step {activeStep} of {totalSteps}</div>
      <div className="inspector-title-row">
        <span className="inspector-icon"><Icon size={25} strokeWidth={1.7} /></span>
        <div>
          <h2>{stage.title}</h2>
          <p>{stage.subtitle}</p>
        </div>
      </div>

      <section className="inspector-section">
        <h3>What happens</h3>
        <p>{stage.what}</p>
      </section>

      <section className="inspector-section safety-section">
        <h3>Why it is safe</h3>
        <ul>
          {stage.why.map((item) => (
            <li key={item}><Check size={15} /> <span>{item}</span></li>
          ))}
        </ul>
      </section>

      <section className="inspector-section">
        <h3>Code references</h3>
        <div className="code-reference-list">
          {stage.code.map((reference) => (
            <div className="code-reference" key={`${reference.path}:${reference.line}`}>
              <Code2 size={14} />
              <span>
                <code>{reference.path}:{reference.line}</code>
                <small>{reference.symbol}</small>
              </span>
            </div>
          ))}
        </div>
      </section>

      <section className="payload-section">
        <div className="payload-heading">
          <span><Braces size={15} /> Step context</span>
          <button type="button" onClick={copyPayload} aria-label="Copy step context">
            {copied ? <Check size={14} /> : <Copy size={14} />}
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
        <pre>{payload}</pre>
      </section>

      <a className="source-link" href="https://github.com/eugenenazirov/data-analysis-chat-assistant" target="_blank" rel="noreferrer">
        View repository <ExternalLink size={14} />
      </a>
    </aside>
  )
}
