import { useEffect, useState } from "preact/hooks";
import { engineStatusWord, worstScheduledTier } from "../lib/system";
import { start, state, status, stop, lastOkAt } from "../lib/systemStore";

export function StatusRail() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    start();
    const clock = window.setInterval(() => setNow(Date.now()), 5_000);
    return () => {
      window.clearInterval(clock);
      stop();
    };
  }, []);

  const snapshot = status.value;
  const last = lastOkAt.value;
  const failed = state.value === "error";
  // A snapshot older than a minute (two missed 30s polls) is stale even if no
  // poll has errored yet; the local clock ages hung requests out too.
  const stale = last !== null && now - last > 60_000;
  const healthy =
    snapshot !== null &&
    !failed &&
    !stale &&
    engineStatusWord(worstScheduledTier(snapshot.loops)) === "nominal";
  const label = failed
    ? "System unavailable"
    : stale
      ? "System status stale"
      : snapshot === null
        ? "Checking system"
        : healthy
          ? "System healthy"
          : "System needs attention";
  const detail =
    last === null
      ? "No successful status reading"
      : `Last successful reading: ${new Date(last).toLocaleString()}`;

  return (
    <a
      href="/system"
      title={detail}
      class={`system-pill ${healthy ? "" : "system-pill-attention"}`}
    >
      <span class="status-dot-simple" aria-hidden="true" />
      {label}
    </a>
  );
}
