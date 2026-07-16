import { useState } from 'react'
import { Header } from './components/Header'
import { scenarios, scenarioMap } from './data/scenarios'
import type { AppTab } from './types'
import { ArchitectureView } from './views/ArchitectureView'
import { GuardrailsView } from './views/GuardrailsView'
import { RouteView } from './views/RouteView'
import { TelemetryView } from './views/TelemetryView'

function App() {
  const [activeTab, setActiveTab] = useState<AppTab>('route')
  const [scenarioId, setScenarioId] = useState(scenarios[0].id)
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const scenario = scenarioMap.get(scenarioId) ?? scenarios[0]

  const changeTab = (tab: AppTab) => {
    setActiveTab(tab)
    setMobileMenuOpen(false)
  }

  return (
    <div className="app-shell">
      <Header
        activeTab={activeTab}
        onTabChange={changeTab}
        mobileMenuOpen={mobileMenuOpen}
        onToggleMobileMenu={() => setMobileMenuOpen((value) => !value)}
      />
      {activeTab === 'route' ? (
        <RouteView scenario={scenario} scenarios={scenarios} onScenarioChange={setScenarioId} />
      ) : null}
      {activeTab === 'architecture' ? <ArchitectureView /> : null}
      {activeTab === 'guardrails' ? <GuardrailsView /> : null}
      {activeTab === 'telemetry' ? (
        <TelemetryView scenario={scenario} scenarios={scenarios} onScenarioChange={setScenarioId} />
      ) : null}
    </div>
  )
}

export default App
