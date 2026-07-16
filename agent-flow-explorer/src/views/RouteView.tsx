import { CheckCircle2, Play } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { EventStream } from '../components/EventStream'
import { FlowCanvas } from '../components/FlowCanvas'
import { Inspector } from '../components/Inspector'
import { PlaybackControls } from '../components/PlaybackControls'
import { ScenarioRail } from '../components/ScenarioRail'
import type { Scenario } from '../types'

type RouteViewProps = {
  scenario: Scenario
  scenarios: Scenario[]
  onScenarioChange: (id: string) => void
}

export function RouteView({ scenario, scenarios, onScenarioChange }: RouteViewProps) {
  const playableStageIndexes = useMemo(
    () => scenario.stages
      .map((stage, index) => (stage.outcome === 'skipped' ? -1 : index))
      .filter((index) => index >= 0),
    [scenario.stages],
  )
  const initialPosition = Math.min(3, playableStageIndexes.length - 1)
  const initialStageIndex = playableStageIndexes[initialPosition]
  const [position, setPosition] = useState(initialPosition)
  const [selectedStageId, setSelectedStageId] = useState(
    scenario.stages[initialStageIndex].id,
  )
  const [isPlaying, setIsPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const previousScenarioId = useRef(scenario.id)

  const scenarioChanged = previousScenarioId.current !== scenario.id
  const visiblePosition = scenarioChanged
    ? 0
    : Math.min(position, playableStageIndexes.length - 1)
  const activeStageIndex = playableStageIndexes[visiblePosition]
  const activeStage = scenario.stages[activeStageIndex]
  const selectedStage = scenario.stages.find((stage) => stage.id === selectedStageId) ?? activeStage

  useEffect(() => {
    if (previousScenarioId.current === scenario.id) return
    previousScenarioId.current = scenario.id
    setPosition(0)
    setSelectedStageId(scenario.stages[0].id)
    setIsPlaying(false)
  }, [scenario.id, scenario.stages])

  useEffect(() => {
    if (!isPlaying) return
    if (position >= playableStageIndexes.length - 1) {
      setIsPlaying(false)
      return
    }
    const timer = window.setTimeout(() => {
      setPosition((current) => current + 1)
    }, 1500 * speed)
    return () => window.clearTimeout(timer)
  }, [isPlaying, playableStageIndexes.length, position, speed])

  useEffect(() => {
    setSelectedStageId(activeStage.id)
  }, [activeStage.id])

  const visibleStageIds = useMemo(
    () => new Set(
      scenario.stages
        .slice(0, activeStageIndex + 1)
        .filter((stage) => stage.outcome !== 'skipped')
        .map((stage) => stage.id),
    ),
    [activeStageIndex, scenario.stages],
  )

  const reset = () => {
    setPosition(0)
    setIsPlaying(false)
  }

  return (
    <main className="route-shell">
      <ScenarioRail
        scenarios={scenarios}
        selectedId={scenario.id}
        onSelect={onScenarioChange}
      />

      <section className="route-main">
        <header className="route-heading">
          <div>
            <h1>{scenario.title}</h1>
            <p className="route-formula">request → route → tools → evidence → report</p>
          </div>
          <button
            className="play-request-button"
            type="button"
            onClick={() => {
              if (position === playableStageIndexes.length - 1) setPosition(0)
              setIsPlaying(true)
            }}
          >
            <Play size={18} fill="currentColor" /> Play request
          </button>
        </header>

        <div className="question-strip">
          <span>Question</span>
          <p>“{scenario.question}”</p>
        </div>

        <FlowCanvas
          stages={scenario.stages}
          activeStageIndex={activeStageIndex}
          selectedStageId={selectedStageId}
          onSelectStage={setSelectedStageId}
        />

        <PlaybackControls
          current={visiblePosition}
          total={playableStageIndexes.length}
          playableStageNumbers={playableStageIndexes.map((index) => index + 1)}
          isPlaying={isPlaying}
          speed={speed}
          onTogglePlay={() => {
            if (!isPlaying && position === playableStageIndexes.length - 1) setPosition(0)
            setIsPlaying((value) => !value)
          }}
          onPrevious={() => setPosition((current) => Math.max(0, current - 1))}
          onNext={() => setPosition((current) => Math.min(playableStageIndexes.length - 1, current + 1))}
          onReset={reset}
          onSpeedChange={setSpeed}
          onJump={setPosition}
        />

        <EventStream
          events={scenario.events}
          visibleStageIds={visibleStageIds}
          selectedStageId={selectedStageId}
          onSelectStage={setSelectedStageId}
        />

        <section className="scenario-outcome">
          <CheckCircle2 size={19} />
          <div><small>Scenario outcome</small><strong>{scenario.result}</strong></div>
          <p>{scenario.takeaway}</p>
        </section>
      </section>

      <Inspector
        stage={selectedStage}
        activeStep={visiblePosition + 1}
        totalSteps={playableStageIndexes.length}
      />
    </main>
  )
}
