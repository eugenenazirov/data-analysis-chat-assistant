import {
  Activity,
  ChartNetwork,
  GitBranch,
  Layers3,
  Menu,
  ShieldCheck,
  X,
} from 'lucide-react'
import type { AppTab } from '../types'

const tabs = [
  { id: 'route', label: 'Request Flow', icon: GitBranch },
  { id: 'architecture', label: 'Architecture', icon: Layers3 },
  { id: 'guardrails', label: 'Guardrails', icon: ShieldCheck },
  { id: 'telemetry', label: 'Telemetry', icon: Activity },
] as const

type HeaderProps = {
  activeTab: AppTab
  onTabChange: (tab: AppTab) => void
  mobileMenuOpen: boolean
  onToggleMobileMenu: () => void
}

export function Header({
  activeTab,
  onTabChange,
  mobileMenuOpen,
  onToggleMobileMenu,
}: HeaderProps) {
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          <ChartNetwork size={20} strokeWidth={1.8} />
        </span>
        <span className="brand-name">Agent Flight Deck</span>
        <span className="brand-divider" />
        <span className="brand-subtitle">Inside a single request</span>
      </div>

      <button
        className="mobile-menu-button"
        type="button"
        aria-label={mobileMenuOpen ? 'Close navigation' : 'Open navigation'}
        aria-expanded={mobileMenuOpen}
        onClick={onToggleMobileMenu}
      >
        {mobileMenuOpen ? <X size={21} /> : <Menu size={21} />}
      </button>

      <nav className={`primary-nav ${mobileMenuOpen ? 'is-open' : ''}`} aria-label="Sections">
        {tabs.map((tab) => {
          const Icon = tab.icon
          return (
            <button
              key={tab.id}
              type="button"
              className={`nav-item ${activeTab === tab.id ? 'is-active' : ''}`}
              onClick={() => onTabChange(tab.id)}
            >
              <Icon size={17} strokeWidth={1.8} />
              <span>{tab.label}</span>
            </button>
          )
        })}
      </nav>
    </header>
  )
}
