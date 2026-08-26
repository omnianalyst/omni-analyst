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
    mix: "25% each: VTI, GLD, SGOV, TLT",
    cagr: 6.9,
    worstYear: -11.8,
    worstYearWhen: 2022,
    years: 7,
    accent: "var(--tier-fresh)",
  },
  {
    key: "aggressive",
    title: "Aggressive",
    thesis: "The mix the measured decade rewarded most, per unit of worst year.",
    mix: "20% BTC, 30% QQQ, 20% GLD, 10% TSLA, 10% LLY, 10% PGR",
    cagr: 41.3,
    worstYear: -23.2,
    worstYearWhen: 2022,
    years: 11,
    accent: "var(--tier-aging)",
  },
];

const DOMINATED: Array<{ label: string; mix: string; cagr: number; worstYear: number; why: string }> = [
  {
    label: "The middle compromise",
    mix: "35% VTI, 15% QQQ, 15% GLD, 15% TLT, 10% BTC, 10% TSLA",
    cagr: 27.6,
    worstYear: -29.6,
    why: "measured a deeper worst year than Aggressive with less return -- compromising bought nothing",
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
      <header class="verdict-head">
        <h1>The verdict</h1>
        <p>
          Every mix below is equal weight, rebalanced each year, measured on daily
          closes 2015&ndash;2026 ({AS_OF}). Two mixes survived the comparison on
          every axis; the rest are shown below them, beaten but not hidden.
          History, not a promise -- and the Aggressive edge is BTC's decade,
          which is the only decade we have.
        </p>
      </header>

      <div class="verdict-grid">
        {VERDICTS.map((v) => (
          <article class="verdict-card" key={v.key} style={`border-top-color:${v.accent}`}>
            <header>
              <span style={`color:${v.accent}`}>{v.title}</span>
              <small>{v.thesis}</small>
            </header>
            <p class="verdict-mix">{v.mix}</p>
            <dl class="verdict-facts">
              <div>
                <dt>Returned per year</dt>
                <dd>{v.cagr.toFixed(1)}%</dd>
              </div>
              <div>
                <dt>Worst year</dt>
                <dd class="value-negative">{v.worstYear.toFixed(1)}% ({v.worstYearWhen})</dd>
              </div>
              <div>
                <dt>Measured over</dt>
                <dd>{v.years} years</dd>
              </div>
            </dl>
          </article>
        ))}
      </div>

      <section class="verdict-dominated">
        <h2>Measured off the pace</h2>
        {DOMINATED.map((d) => (
          <p key={d.label}>
            <strong>{d.label}</strong> ({d.mix}): {d.cagr.toFixed(1)}%/yr, worst year{" "}
            {d.worstYear.toFixed(1)}% -- {d.why}.
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
