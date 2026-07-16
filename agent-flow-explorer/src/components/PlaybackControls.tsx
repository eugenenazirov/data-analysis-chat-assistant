import { Pause, Play, RotateCcw, SkipBack, SkipForward } from 'lucide-react'

type PlaybackControlsProps = {
  current: number
  total: number
  playableStageNumbers: number[]
  isPlaying: boolean
  speed: number
  onTogglePlay: () => void
  onPrevious: () => void
  onNext: () => void
  onReset: () => void
  onSpeedChange: (speed: number) => void
  onJump: (position: number) => void
}

export function PlaybackControls({
  current,
  total,
  playableStageNumbers,
  isPlaying,
  speed,
  onTogglePlay,
  onPrevious,
  onNext,
  onReset,
  onSpeedChange,
  onJump,
}: PlaybackControlsProps) {
  return (
    <div className="playback-bar">
      <div className="transport-controls">
        <button type="button" onClick={onPrevious} aria-label="Previous step" disabled={current === 0}>
          <SkipBack size={17} />
        </button>
        <button className="primary-transport" type="button" onClick={onTogglePlay} aria-label={isPlaying ? 'Pause' : 'Play request'}>
          {isPlaying ? <Pause size={17} fill="currentColor" /> : <Play size={17} fill="currentColor" />}
        </button>
        <button type="button" onClick={onNext} aria-label="Next step" disabled={current === total - 1}>
          <SkipForward size={17} />
        </button>
      </div>

      <label className="speed-control">
        <span>Speed</span>
        <select value={speed} onChange={(event) => onSpeedChange(Number(event.target.value))}>
          <option value={1.5}>0.75×</option>
          <option value={1}>1×</option>
          <option value={0.65}>1.5×</option>
          <option value={0.4}>2×</option>
        </select>
      </label>

      <button className="reset-control" type="button" onClick={onReset}>
        Reset <RotateCcw size={14} />
      </button>

      <div className="progress-wrap">
        <span className="progress-label">Step {current + 1} of {total}</span>
        <div className="progress-track" role="group" aria-label="Jump to step">
          {playableStageNumbers.map((stageNumber, position) => (
            <button
              key={stageNumber}
              type="button"
              className={position < current ? 'is-complete' : position === current ? 'is-current' : ''}
              onClick={() => onJump(position)}
              aria-label={`Jump to step ${stageNumber}`}
            >
              <span>{stageNumber}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
