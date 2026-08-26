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
  years: number;
  accent: string;
}

const VERDICTS: Verdict[] = [
  {
    key: "steady",
    title: "Steady",
    thesis: "The mix that never asked you to be brave.",
    mix: "25% each · VTI · GLD · SGOV · TLT",
    cagr: 6.9,
    worstYear: -11.8,
    worstYearWhen: 2022,
    years: 7,
    accent: "#2dd4bf",
  },
  {
    key: "aggressive",
    title: "Aggressive",
    thesis: "The mix the measured decade rewarded most, per unit of worst year.",
    mix: "20% BTC · 30% QQQ · 20% GLD · 10% TSLA · 10% LLY · 10% PGR",
    cagr: 41.3,
    worstYear: -23.2,
    worstYearWhen: 2022,
    years: 11,
    accent: "#fbbf24",
  },
];

const DOMINATED: Array<{ label: string; mix: string; cagr: number; worstYear: number; why: string }> = [
  {
    label: "The middle compromise",
    mix: "35% VTI, 15% QQQ, 15% GLD, 15% TLT, 10% BTC, 10% TSLA",
    cagr: 27.6,
    worstYear: -29.6,
    why: "a deeper worst year than Aggressive, with less return -- compromising bought nothing",
  },
  {
    label: "The famous stocks",
    mix: "Mag 7, equal weight",
    cagr: 32.7,
    worstYear: -47.3,
    why: "the household names cost a -47% year to make less than Aggressive made",
  },
];

export function VerdictView() {
  return (
    <div class="verdict-view">
      <header class="v-hero">
        <p class="v-kicker">The verdict · measured 2015&ndash;2026</p>
        <h2 class="v-line">Two mixes survived every comparison.</h2>
        <p class="v-note">
          Equal weight, rebalanced each year, daily closes. History, not a promise --
          and the Aggressive edge is BTC's decade, the only decade we have.
          As of {AS_OF}.
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
              per year · worst year {v.worstYear.toFixed(1)}% ({v.worstYearWhen}) · {v.years}y measured
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
