import { useEffect } from "preact/hooks";
import {
  engineStatusWord,
  fillOutcomeClass,
  loopAgeLabel,
  loopCadence,
  scheduledLoopTier,
  worstScheduledTier,
  type EngineStatusWord,
  type LoopStatus,
} from "../lib/system";
import {
  lastOkAt,
  refresh,
  start,
  state,
  status,
  stop,
} from "../lib/systemStore";

const WORD_TIER: Record<EngineStatusWord, string> = {
  nominal: "fresh",
  degraded: "aging",
  stalled: "stale",
  down: "dead",
  standby: "unknown",
};

function staleLabel(): string | null {
  if (lastOkAt.value === null) return null;
  const mins = Math.max(0, Math.round((Date.now() - lastOkAt.value) / 60_000));
  if (mins < 1) return "stale snapshot";
  return `stale snapshot ${mins}m old`;
}

function FillCounts({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts);
  const total = entries.reduce((sum, [, n]) => sum + n, 0);
  // No fills in the last hour means the engine is idle on that path, not that
  // fill data is missing. Omit the group rather than render a hollow "0".
  if (total === 0) return null;
  return (
    <span class="bar-stat">
      {entries.map(([outcome, n]) => (
        <span key={outcome} class={`fill fill-${fillOutcomeClass(outcome)}`}>
          {n}
          {outcome[0]}
        </span>
      ))}
    </span>
  );
}

function LoopDot({ loop }: { loop: LoopStatus }) {
  const tier = scheduledLoopTier(loop.age_seconds, loop.never_run);
  return (
    <span class="loop" title={`${loop.loop}: ${loopAgeLabel(loop)}`}>
      <span class={`engine-dot tier-${tier}`} aria-hidden="true" />
      {loop.loop.slice(0, 4)}
      <span class="loop-age">{loopAgeLabel(loop)}</span>
    </span>
  );
}

export function StatusRail() {
  useEffect(() => {
    start();
    return () => stop();
  }, []);

  const s = status.value;
  const st = state.value;

  // First-load, no data yet: honest placeholder, never fabricated numbers.
  if (s === null) {
    if (st === "error") {
      return (
        <div class="systembar systembar-error">
          <span class="engine tier-dead">
            <span class="engine-dot" aria-hidden="true" /> status unavailable
          </span>
          <button type="button" class="bar-retry" onClick={() => void refresh()}>
            retry
          </button>
          <a class="bar-detail" href="/system">details</a>
        </div>
      );
    }
    return (
      <div class="systembar">
        <span class="engine tier-unknown">
          <span class="engine-dot engine-dot-pulse" aria-hidden="true" /> connecting
        </span>
      </div>
    );
  }

  const worst = worstScheduledTier(s.loops);
  const word = engineStatusWord(worst);
  const tierClass = WORD_TIER[word];
  const scheduled = s.loops.filter((l) => loopCadence(l.loop) === "scheduled");
  const stale = st === "error" ? staleLabel() : null;

  return (
    <div class={`systembar ${stale ? "systembar-stale" : ""}`}>
      <div class="systembar-left">
        <span class={`engine tier-${tierClass}`}>
          <span class="engine-dot" aria-hidden="true" /> {word}
        </span>
        {scheduled.map((l) => (
          <LoopDot key={l.loop} loop={l} />
        ))}
        {stale ? <span class="bar-stale">{stale}</span> : null}
      </div>
      <div class="systembar-right">
        <span class="bar-stat">
          demand <strong>{s.demand.active}</strong>
          <span class="bar-dim">/{s.demand.total}</span>
        </span>
        <FillCounts counts={s.fill_last_hour} />
        <span class="bar-stat">
          24h <strong>{s.production_24h.predictions}</strong>p
          <span class="bar-dim">/{s.production_24h.findings}f</span>
        </span>
        <a class="bar-detail" href="/system">details</a>
      </div>
    </div>
  );
}
