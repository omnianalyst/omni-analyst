import { useEffect, useState } from "preact/hooks";
import { getRegime, type RegimeValue } from "../lib/autonomous";
import { regimeHeadline, regimeImplication } from "../lib/explain";

// The canopy above the picks: a one-line plain-English read of the macro regime
// and the generic, honest, non-advice implication of it. This is the "understand"
// entry that frames every pick below it -- a novice lands on a human sentence
// about the market, not a wall of metrics. When no regime has been assessed yet
// the banner says so, because silence-as-honesty is the product, not an absence.

type State =
  | { kind: "loading" }
  | { kind: "ok"; value: RegimeValue; date: string | null }
  | { kind: "none" };

export function MarketBanner() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const r = await getRegime();
        if (cancelled) return;
        if (r && r.value) {
          setState({ kind: "ok", value: r.value, date: r.knowledge_date });
        } else {
          setState({ kind: "none" });
        }
      } catch {
        if (!cancelled) setState({ kind: "none" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "loading") {
    return (
      <section class="market-banner market-banner-loading">
        <p class="market-headline">Reading the market&hellip;</p>
      </section>
    );
  }

  if (state.kind === "none") {
    return (
      <section class="market-banner market-banner-none">
        <p class="market-headline">Market regime is still being assessed</p>
        <p class="market-implication">
          {regimeImplication(null)} The picks below are the system&apos;s calibrated
          calls regardless.
        </p>
      </section>
    );
  }

  const v = state.value;
  return (
    <section class="market-banner">
      <p class="market-headline">{regimeHeadline(v)}</p>
      <p class="market-implication">{regimeImplication(v)}</p>
      {state.date ? (
        <p class="market-date">Assessed {state.date.slice(0, 10)} from FRED macro data</p>
      ) : null}
    </section>
  );
}
