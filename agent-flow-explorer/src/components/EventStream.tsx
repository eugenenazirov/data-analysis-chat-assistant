import { CheckCircle2, CircleAlert, CircleX, Radio } from 'lucide-react'
import type { FlightEvent } from '../types'

type EventStreamProps = {
  events: FlightEvent[]
  visibleStageIds: Set<string>
  selectedStageId?: string
  onSelectStage?: (id: string) => void
}

const levelIcons = {
  info: Radio,
  ok: CheckCircle2,
  warn: CircleAlert,
  error: CircleX,
}

export function EventStream({
  events,
  visibleStageIds,
  selectedStageId,
  onSelectStage,
}: EventStreamProps) {
  const visibleEvents = events.filter((item) => visibleStageIds.has(item.stageId))
  return (
    <section className="event-stream" aria-label="Event stream and evidence">
      <div className="event-stream-heading">
        <span>Event stream / Evidence</span>
        <small>{visibleEvents.length} of {events.length} events</small>
      </div>
      <div className="event-rows">
        {visibleEvents.map((item) => {
          const Icon = levelIcons[item.level]
          return (
            <button
              type="button"
              key={item.id}
              className={`event-row level-${item.level} ${selectedStageId === item.stageId ? 'is-selected' : ''}`}
              onClick={() => onSelectStage?.(item.stageId)}
            >
              <span className="event-time">{item.offset}</span>
              <span className="event-level"><Icon size={13} /> {item.level.toUpperCase()}</span>
              <span className="event-source">{item.source}</span>
              <strong>{item.event}</strong>
              <span className="event-detail">{item.detail}</span>
            </button>
          )
        })}
      </div>
    </section>
  )
}
