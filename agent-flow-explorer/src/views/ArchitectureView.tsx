import { ArrowDown, ArrowRight, Box, Check, GitPullRequestArrow } from 'lucide-react'
import { useState } from 'react'
import { architectureLayers, runtimeComposition, systemFacts } from '../data/architecture'

export function ArchitectureView() {
  const [selectedLayer, setSelectedLayer] = useState('application')
  const activeLayer = architectureLayers.find((layer) => layer.id === selectedLayer)!
  const ActiveIcon = activeLayer.icon

  return (
    <main className="learning-view architecture-view">
      <header className="view-intro">
        <div>
          <h1>Clean Architecture without decorative layers</h1>
          <p>
            Select a layer to inspect its rule, modules, and concrete responsibility. Dependency
            arrows always point inward, toward application contracts and domain policies.
          </p>
        </div>
        <span className="view-path">CLI → use case → ports → adapters</span>
      </header>

      <section className="architecture-workspace">
        <div className="layer-stack" aria-label="Architecture layers">
          {architectureLayers.map((layer, index) => {
            const Icon = layer.icon
            const selected = selectedLayer === layer.id
            return (
              <div className="layer-with-arrow" key={layer.id}>
                <button
                  type="button"
                  className={`architecture-layer tone-${layer.color} ${selected ? 'is-selected' : ''}`}
                  onClick={() => setSelectedLayer(layer.id)}
                  aria-pressed={selected}
                >
                  <span className="layer-order">{layer.order}</span>
                  <span className="layer-icon"><Icon size={23} strokeWidth={1.7} /></span>
                  <span className="layer-main">
                    <strong>{layer.name}</strong>
                    <small>{layer.role}</small>
                  </span>
                  <ArrowRight size={18} />
                </button>
                {index < architectureLayers.length - 1 ? (
                  <span className="dependency-arrow"><ArrowDown size={16} /> imports point inward</span>
                ) : null}
              </div>
            )
          })}
        </div>

        <aside className={`layer-inspector tone-${activeLayer.color}`}>
          <div className="layer-inspector-title">
            <ActiveIcon size={27} />
            <div><small>Selected layer</small><h2>{activeLayer.name}</h2></div>
          </div>
          <p>{activeLayer.role}</p>
          <div className="layer-rule"><Check size={16} /> <span>{activeLayer.rule}</span></div>
          <h3>Core modules</h3>
          <div className="module-list">
            {activeLayer.modules.map((module) => (
              <code key={module}>retail_agent/{activeLayer.id}/{module}</code>
            ))}
          </div>
        </aside>
      </section>

      <section className="composition-section">
        <div className="section-heading">
          <div><GitPullRequestArrow size={20} /><h2>Runtime has a single composition root</h2></div>
          <p><code>retail_agent/bootstrap.py</code> binds ports to concrete adapters.</p>
        </div>
        <div className="composition-line">
          {runtimeComposition.map((item, index) => {
            const Icon = item.icon
            return (
              <div className="composition-item" key={item.label}>
                <span className="composition-box">
                  <Icon size={19} />
                  <small>{item.label}</small>
                  <strong>{item.value}</strong>
                </span>
                {index < runtimeComposition.length - 1 ? <ArrowRight size={18} /> : null}
              </div>
            )
          })}
        </div>
      </section>

      <section className="fact-strip">
        {systemFacts.map((fact) => {
          const Icon = fact.icon
          return <span key={fact.label}><Icon size={17} /><small>{fact.label}</small><strong>{fact.value}</strong></span>
        })}
      </section>

      <div className="architecture-note">
        <Box size={18} />
        <p>
          Root-level compatibility modules preserve legacy imports while responsibilities remain
          separated. The AST test <code>tests/architecture/test_dependency_boundaries.py</code>{' '}
          enforces dependency direction automatically.
        </p>
      </div>
    </main>
  )
}
