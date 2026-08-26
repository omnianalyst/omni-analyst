// The verdict page. One screen: the mixes the measured decade actually
// rewarded, stated with their numbers, nothing to configure.
//
// Provenance: equal-weight, rebalanced to target each calendar year, daily
// closes from the same display feed the scanner reads, window 2015-08 to
// 2026-08 (Steady 7y: SGOV lists 2020). Worst year = worst calendar year of
// the rebalanced mix. Computed 2026-08-26; frozen like DECISION_TABLE, with
// the same rule: measured history, not a promise.
//
// The shape of the result: by this decade's data, the efficient choices are
// the two poles -- Steady and Aggressive. The middle compromise and the
// famous-stock mix measured worse on BOTH axes (lower CAGR, deeper worst
// year), so they are shown below the verdicts, dominated but not hidden.
// The Aggressive edge is BTC's decade; that is the only decade we have.

const AS_OF = "2026-08-26";

interface Verdict {
  key: string;
  title: string;
  thesis: string;
  mix: string;
  cagr: number;
  worstYear: number;
  worstYearWhen: number;
  maxDrawdown: number;
  years: number;
  accent: string;
}

// The same selection rule with hindsight removed: each January the mix was
// re-picked from trailing five-year data only, then held for the year.
// 2016-2025, core+sleeve universe. The gap between these numbers and the
// frozen mixes above IS the cost of hindsight -- and the reason no number on
// this page is a forecast.
const WALK_FORWARD: Record<string, number> = {
  steady: 19.2,
  balanced: 22.8,
  aggressive: 24.8,
};

// Block-bootstrap on each tier's measured daily path, 2000 ten-year replays:
// the median multiple and the odds of living through a deep drawdown. The
// replay assumes the decade's distribution repeats -- it can only price
// recurrence, not guarantee it.
const BOOTSTRAP: Record<string, { medianMultiple: number; dd30: number }> = {
  steady: { medianMultiple: 2.7, dd30: 0 },
  balanced: { medianMultiple: 4.2, dd30: 36 },
  aggressive: { medianMultiple: 46.5, dd30: 53 },
};

const VERDICTS: Verdict[] = [
  {
    key: "steady",
    title: "Steady",
    thesis: "Equity, gold, cash and short bonds -- sized so the worst year stays shallow.",
    mix: "40% VTI · 20% GLD · 20% SGOV · 20% IEF",
    cagr: 10.8,
    worstYear: -10.5,
    worstYearWhen: 2022,
    maxDrawdown: -15,
    years: 6,
    accent: "#2dd4bf",
  },
  {
    key: "balanced",
    title: "Balanced",
    thesis: "The classic answer, plus gold's shock absorber.",
    mix: "90% VOO · 10% GLD",
    cagr: 15.2,
    worstYear: -16.3,
    worstYearWhen: 2022,
    maxDrawdown: -31,
    years: 11,
    accent: "#7dd3fc",
  },
  {
    key: "aggressive",
    title: "Aggressive",
    thesis: "The mix the measured decade rewarded most, per unit of worst year.",
    mix: "20% BTC · 30% QQQ · 20% GLD · 10% TSLA · 10% LLY · 10% PGR",
    cagr: 47.2,
    worstYear: -22.1,
    worstYearWhen: 2022,
    maxDrawdown: -31,
    years: 11,
    accent: "#fbbf24",
  },
];

const DOMINATED: Array<{ label: string; mix: string; cagr: number; worstYear: number; why: string }> = [
  {
    label: "The hedged compromise",
    mix: "35% VTI, 15% QQQ, 15% GLD, 15% TLT, 10% BTC, 10% TSLA",
    cagr: 31.5,
    worstYear: -28.5,
    why: "a deeper worst year than Balanced with less return than Aggressive -- hedging both ways bought nothing",
  },
  {
    label: "The famous stocks",
    mix: "Mag 7, equal weight",
    cagr: 38.3,
    worstYear: -45.7,
    why: "the household names cost a -46% year to make less than Aggressive made",
  },
];

export function VerdictView() {
  return (
    <div class="verdict-view">
      <header class="v-hero">
        <p class="v-kicker">The verdict · measured 2015&ndash;2026</p>
        <h2 class="v-line">Pick by the worst year you can sit through.</h2>
        <p class="v-note">
          Equal weight, rebalanced each year, daily closes. Worst year is the calendar
          year; worst moment is the deepest dip between any peak and bottom -- judge by
          the one you would actually sit through. Steady's cash sleeve dates to 2020,
          so its window is shorter. Aggressive moves 39&ndash;47%/yr with rebalance
          timing alone. History, not a promise -- the Aggressive edge is BTC's decade,
          the only decade we have. As of {AS_OF}.
        </p>
      </header>

      {VERDICTS.map((v) => (
        <section class="v-verdict" key={v.key} style={`--v-accent:${v.accent}`}>
          <div class="v-left">
            <span class="v-title">{v.title}</span>
            <p class="v-thesis">{v.thesis}</p>
            <p class="v-mix">{v.mix}</p>
          </div>
          <div class="v-right">
            <span class="v-big">
              {v.cagr.toFixed(1)}
              <small>%</small>
            </span>
            <p class="v-under">
              per year · worst year {v.worstYear.toFixed(1)}% ({v.worstYearWhen}) · worst moment{" "}
              {v.maxDrawdown.toFixed(0)}% · {v.years}y measured
            </p>
            <p class="v-under v-under-quiet">
              no hindsight: {WALK_FORWARD[v.key].toFixed(1)}%/yr · replay median x{BOOTSTRAP[v.key].medianMultiple} ·{" "}
              {BOOTSTRAP[v.key].dd30}% odds of a -30% moment
            </p>
          </div>
        </section>
      ))}

      <section class="v-dominated">
        <p class="v-dom-kicker">Measured off the pace</p>
        {DOMINATED.map((d) => (
          <p class="v-dom-line" key={d.label}>
            <strong>{d.label}</strong> -- {d.mix} -- {d.cagr.toFixed(1)}%/yr, worst year{" "}
            {d.worstYear.toFixed(1)}%: {d.why}.
          </p>
        ))}
      </section>

      <nav class="verdict-links">
        <a class="btn-secondary compact-button" href="/rankings">Every measured asset</a>
        <a class="btn-secondary compact-button" href="/map">The map</a>
      </nav>
    </div>
  );
}
