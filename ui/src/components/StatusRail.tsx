import { useEffect } from "preact/hooks";
import { engineStatusWord, worstScheduledTier } from "../lib/system";
import { start, state, status, stop } from "../lib/systemStore";

export function StatusRail() {
  useEffect(() => {
    start();
    return () => stop();
  }, []);

  const snapshot = status.value;
  const storeState = state.value;

  if (snapshot === null) {
    return (
      <a class={`system-pill ${storeState === "error" ? "system-pill-attention" : ""}`} href="/system">
        <span class="status-dot-simple" aria-hidden="true" />
        {storeState === "error" ? "System unavailable" : "Checking system"}
      </a>
    );
  }

  const word = engineStatusWord(worstScheduledTier(snapshot.loops));
  const healthy = word === "nominal";
  return (
    <a class={`system-pill ${healthy ? "" : "system-pill-attention"}`} href="/system">
      <span class="status-dot-simple" aria-hidden="true" />
      {healthy ? "System healthy" : "System needs attention"}
    </a>
  );
}
