import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import { formatHitRate } from "../lib/briefing";
import {
  ABSENT,
  describeCheckedAt,
  describeDivergenceKind,
  describeReconciliation,
  formatDecimal,
  formatQuantity,
  formatTimestamp,
  getEligibility,
  getPortfolio,
  getReconciliation,
  positionSide,
  presentEligibility,
  refusalLabel,
  sideLabel,
  sortVenuesBySeverity,
  unresolvedVenues,
  type EligibilityReport,
  type MethodEligibility,
  type Portfolio,
  type PositionSide,
  type ReconciliationReport,
  type StatusTone,
} from "../lib/trading";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

type Panel<T> =
  | { kind: "loading" }
  | { kind: "auth" }
  | { kind: "ok"; data: T }
  | { kind: "error"; message: string; detail?: string };

// Each panel loads on its own. A portfolio that does not exist answers 404, and
// that must not blank the eligibility panel beside it -- the two report on
// different things and one being absent is not evidence about the other.
function usePanel<T>(load: () => Promise<T>): Panel<T> {
  const [state, setState] = useState<Panel<T>>({ kind: "loading" });
  useEffect(() => {
    let cancelled = false;
    load()
      .then((data) => {
        if (!cancelled) setState({ kind: "ok", data });
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof AuthRequiredError) {
          setState({ kind: "auth" });
          return;
        }
        const { message, detail } = describeError(err);
        setState({ kind: "error", message, detail });
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return state;
}

export function TradingView() {
  const portfolio = usePanel<Portfolio>(getPortfolio);
  const reconciliation = usePanel<ReconciliationReport>(getReconciliation);
  const eligibility = usePanel<EligibilityReport>(() => getEligibility());

  return (
    <div class="trading-view">
      <header class="page-head">
        <h1>Trading</h1>
        <p class="muted">
          What the book holds, whether the venues agree with it, and which
          methods have earned the right to hold capital. Every figure on this
          page is the server&rsquo;s; nothing here is recomputed.
        </p>
      </header>

      <section class="panel">
        <h2 class="panel-title">Eligibility</h2>
        <PanelBody state={eligibility} what="eligibility">
          {(data) => <Eligibility data={data} />}
        </PanelBody>
      </section>

      <section class="panel">
        <h2 class="panel-title">Portfolio</h2>
        <PanelBody state={portfolio} what="the portfolio">
          {(data) => <PortfolioBlock data={data} />}
        </PanelBody>
      </section>

      <section class="panel">
        <h2 class="panel-title">Reconciliation</h2>
        <PanelBody state={reconciliation} what="reconciliation">
          {(data) => <Reconciliation data={data} />}
        </PanelBody>
      </section>
    </div>
  );
}

function PanelBody<T>({
  state,
  what,
  children,
}: {
  state: Panel<T>;
  what: string;
  children: (data: T) => preact.ComponentChildren;
}) {
  if (state.kind === "loading") return <Loading label={`Loading ${what}\u2026`} />;
  if (state.kind === "auth") {
    return (
      <div style={{ padding: "18px" }}>
        <p>
          Trading state is private to its owner. A request without a verified
          token is refused with 401, and rendering an empty panel would pretend
          the server answered when it did not.
        </p>
        <p style={{ marginTop: "12px" }}>
          <a class="search-btn" href="/login" style={{ textDecoration: "none" }}>
            Sign in
          </a>
        </p>
      </div>
    );
  }
  if (state.kind === "error") {
    return <ErrorState message={state.message} detail={state.detail} />;
  }
  return <>{children(state.data)}</>;
}

const absentStyle = { color: "var(--faint)" } as const;

function Figure({
  label,
  value,
  note,
}: {
  label: string;
  value: string | null;
  note?: string;
}) {
  const absent = value === null;
  return (
    <div class="metric">
      <span class="metric-label">{label}</span>
      <span class="metric-value" style={absent ? absentStyle : undefined}>
        {formatDecimal(value)}
      </span>
      {absent ? (
        <span class="metric-sub">not reported</span>
      ) : note ? (
        <span class="metric-sub">{note}</span>
      ) : null}
    </div>
  );
}

const SIDE_STYLE: Record<PositionSide, Record<string, string>> = {
  long: { color: "var(--muted)" },
  short: { color: "var(--accent)", borderColor: "var(--accent)" },
  flat: { color: "var(--faint)" },
  unknown: { color: "var(--tier-aging)", borderColor: "var(--tier-aging)" },
  contradictory: { color: "var(--tier-dead)", borderColor: "var(--tier-dead)" },
};

function PortfolioBlock({ data }: { data: Portfolio }) {
  return (
    <>
      <div class="metric-grid" style={{ padding: "0 0 18px" }}>
        <Figure label="NAV" value={data.nav} />
        <Figure label="Cash" value={data.cash} />
        <Figure label="Gross exposure" value={data.gross_exposure} />
        <Figure label="Net exposure" value={data.net_exposure} />
      </div>
      <p class="gap-meta" style={{ paddingBottom: "12px" }}>
        <span class="faint">portfolio {data.portfolio_id}</span>
        <span class="faint">as of {formatTimestamp(data.as_of)}</span>
      </p>

      <h3 class="panel-sub">Positions</h3>
      {data.positions.length === 0 ? (
        <p class="empty">
          This portfolio holds no positions. The state was read and it is empty
          &mdash; a different fact from a portfolio that could not be read.
        </p>
      ) : (
        <table class="coverage">
          <thead>
            <tr>
              <th>Venue</th>
              <th>Symbol</th>
              <th>Market</th>
              <th>Side</th>
              <th class="num">Quantity</th>
              <th class="num">Average entry</th>
              <th class="num">Notional</th>
              <th>As of</th>
            </tr>
          </thead>
          <tbody>
            {data.positions.map((p) => {
              const side = positionSide(p);
              return (
                <tr key={`${p.venue}:${p.symbol}:${p.market_type}`}>
                  <td>{p.venue}</td>
                  <td class="claim-type">{p.symbol}</td>
                  <td class="faint">{p.market_type}</td>
                  <td>
                    <span class="badge" style={SIDE_STYLE[side]}>
                      {sideLabel(side)}
                    </span>
                  </td>
                  <td class="num" style={{ color: SIDE_STYLE[side].color }}>
                    {formatQuantity(p.quantity)}
                  </td>
                  <td
                    class="num"
                    style={p.average_entry === null ? absentStyle : undefined}
                  >
                    {formatDecimal(p.average_entry)}
                  </td>
                  <td
                    class="num"
                    style={p.notional === null ? absentStyle : undefined}
                  >
                    {formatDecimal(p.notional)}
                  </td>
                  <td class="faint">{formatTimestamp(p.as_of)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <h3 class="panel-sub" style={{ marginTop: "18px" }}>
        Cash balances
      </h3>
      {data.cash_positions.length === 0 ? (
        <p class="empty">No cash balances have been recorded for this portfolio.</p>
      ) : (
        <table class="coverage">
          <thead>
            <tr>
              <th>Venue</th>
              <th>Asset</th>
              <th class="num">Free</th>
              <th class="num">Locked</th>
              <th>As of</th>
            </tr>
          </thead>
          <tbody>
            {data.cash_positions.map((c) => (
              <tr key={`${c.venue}:${c.asset}`}>
                <td>{c.venue}</td>
                <td class="claim-type">{c.asset}</td>
                <td class="num" style={c.free === null ? absentStyle : undefined}>
                  {formatDecimal(c.free)}
                </td>
                <td class="num" style={c.locked === null ? absentStyle : undefined}>
                  {formatDecimal(c.locked)}
                </td>
                <td class="faint">{formatTimestamp(c.as_of)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

const TONE_STYLE: Record<StatusTone, Record<string, string>> = {
  clear: { color: "var(--tier-fresh)", borderColor: "rgba(45, 212, 191, 0.3)" },
  diverged: { color: "var(--tier-dead)", borderColor: "rgba(239, 68, 68, 0.3)" },
  unresolved: { color: "var(--tier-stale)", borderColor: "rgba(249, 115, 22, 0.4)" },
  unknown: { color: "var(--tier-aging)", borderColor: "rgba(234, 179, 8, 0.4)" },
};

function Reconciliation({ data }: { data: ReconciliationReport }) {
  const unresolved = unresolvedVenues(data.venues);
  return (
    <>
      <p class="gap-meta" style={{ paddingBottom: "12px" }}>
        <span class="faint">as of {formatTimestamp(data.as_of)}</span>
      </p>

      {data.venues.length === 0 ? (
        <p class="empty">
          No venue has a reconciliation record. Nothing has been checked, which
          is not the same as nothing being wrong.
        </p>
      ) : null}

      {unresolved.length > 0 ? (
        <p
          class="panel-sub"
          style={{ color: "var(--tier-stale)", paddingBottom: "12px" }}
        >
          Not every venue is settled:{" "}
          {unresolved
            .map((v) => `${v.venue} (${describeReconciliation(v.status).label})`)
            .join(", ")}
          .
        </p>
      ) : null}

      <ul class="gaps">
        {sortVenuesBySeverity(data.venues).map((v) => {
          const p = describeReconciliation(v.status);
          return (
            <li class="gap-row" key={v.venue}>
              <div class="gap-head">
                <span class="gap-type">{v.venue}</span>
                <span class="badge" style={TONE_STYLE[p.tone]}>
                  {p.label}
                </span>
              </div>
              <div class="gap-meta">
                <span class="faint">{describeCheckedAt(v.checked_at)}</span>
              </div>
              <p class="metric-sub" style={{ marginTop: "6px" }}>
                {p.explanation}
              </p>
              {p.tone === "diverged" && v.discrepancies.length === 0 ? (
                <p class="metric-sub" style={{ color: "var(--tier-aging)" }}>
                  The venue is marked diverged but listed no differences, so what
                  disagreed is not stated here.
                </p>
              ) : null}
              {v.discrepancies.length > 0 ? (
                <table class="coverage" style={{ marginTop: "10px" }}>
                  <thead>
                    <tr>
                      <th>Difference</th>
                      <th>Symbol</th>
                      <th class="num">Local</th>
                      <th class="num">At venue</th>
                      <th>Detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {v.discrepancies.map((d, i) => (
                      <tr key={`${d.kind}:${d.symbol ?? ""}:${i}`}>
                        <td class="claim-type">{describeDivergenceKind(d.kind)}</td>
                        <td>{d.symbol ?? <span style={absentStyle}>{ABSENT}</span>}</td>
                        <td class="num" style={d.local === null ? absentStyle : undefined}>
                          {formatDecimal(d.local)}
                        </td>
                        <td class="num" style={d.remote === null ? absentStyle : undefined}>
                          {formatDecimal(d.remote)}
                        </td>
                        <td class="faint">{d.detail ?? ABSENT}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : null}
            </li>
          );
        })}
      </ul>
    </>
  );
}

function Eligibility({ data }: { data: EligibilityReport }) {
  const verdict = presentEligibility(data);
  if (verdict.kind === "unreadable") {
    return <ErrorState message={verdict.headline} detail={verdict.explanation} />;
  }
  const gp = data.gate_parameters;
  return (
    <>
      <h3 class="panel-sub">{verdict.headline}</h3>
      <p class="muted" style={{ padding: "0 0 12px" }}>
        {verdict.explanation}
      </p>

      <div class="gap-meta" style={{ paddingBottom: "12px" }}>
        <span>
          priced at a trade size of <strong>{formatDecimal(data.notional)}</strong>
        </span>
        <span>
          round trip <strong>{formatDecimal(gp.round_trip_cost_bps)} bps</strong> on{" "}
          {gp.cost_venue}
        </span>
        <span>
          minimum net <strong>{formatDecimal(gp.min_expectancy_bps)} bps</strong>
        </span>
        <span>
          minimum effective n <strong>{gp.min_effective_n}</strong>
        </span>
        <span>
          maximum assumed share <strong>{formatDecimal(gp.max_assumed_share)}</strong>
        </span>
        <span>
          maximum concentration <strong>{formatDecimal(gp.max_concentration)}</strong>
        </span>
        <span class="faint">as of {formatTimestamp(data.as_of)}</span>
        {data.venues_are_modelled ? (
          <span class="faint">venue costs are modelled, not quoted</span>
        ) : null}
      </div>

      {verdict.methods.length === 0 ? null : (
        <ul class="gaps">
          {verdict.methods.map((m) => (
            <MethodRow key={`${m.method}:${m.entity_kind}`} method={m} />
          ))}
        </ul>
      )}
    </>
  );
}

function MethodRow({ method: m }: { method: MethodEligibility }) {
  const wf = m.walk_forward;
  return (
    <li class="gap-row">
      <div class="gap-head">
        <span class="gap-type">
          {m.method}
          <span class="gap-key"> &middot; {m.entity_kind}</span>
        </span>
        <span class="badge">{m.status}</span>
      </div>

      <div class="gap-meta">
        <span>
          predictions <strong>{m.total_n}</strong>
        </span>
        <span>
          resolved <strong>{m.resolved_n}</strong>
        </span>
        <span>
          measured <strong>{m.measured_n}</strong>
        </span>
        <span>
          live <strong>{m.live_resolved_n}</strong>
        </span>
        <span>
          hit rate <strong>{formatHitRate(m.hit_rate)}</strong>
        </span>
        {m.hit_rate_interval ? (
          <span class="faint">
            interval {m.hit_rate_interval[0].toFixed(2)} to{" "}
            {m.hit_rate_interval[1].toFixed(2)}
          </span>
        ) : null}
      </div>

      <ul class="gaps" style={{ marginTop: "8px" }}>
        {m.gates.map((g) => (
          <li class="gap-row" key={g.phase} style={{ paddingLeft: 0, paddingRight: 0 }}>
            <div class="gap-head">
              <span class="gap-type" style={{ fontWeight: 400 }}>
                {g.eligible ? "Permitted" : refusalLabel(g.reason)}
              </span>
              <span class={g.eligible ? "badge badge-pos" : "badge"}>{g.phase}</span>
            </div>
            {g.detail ? (
              <div class="gap-meta">
                <span class="faint">{g.detail}</span>
              </div>
            ) : null}
          </li>
        ))}
      </ul>

      <div class="gap-meta" style={{ marginTop: "8px" }}>
        {m.realised.refusal ? (
          <span class="faint">realised edge: {m.realised.refusal}</span>
        ) : (
          <>
            <span>
              realised net <strong>{formatDecimal(m.realised.net_bps)} bps</strong>
            </span>
            <span>
              gross <strong>{formatDecimal(m.realised.gross_bps)} bps</strong>
            </span>
            <span>
              assumed share <strong>{formatDecimal(m.realised.assumed_share)}</strong>
            </span>
            <span>
              concentration <strong>{formatDecimal(m.realised.concentration)}</strong>
            </span>
          </>
        )}
        <span class="faint">
          n {m.realised.n}, effective n {m.realised.effective_n}, entities{" "}
          {m.realised.positive_entities}
        </span>
      </div>

      <div class="gap-meta">
        {m.expectancy.refusal ? (
          <span class="faint">modelled expectancy: {m.expectancy.refusal}</span>
        ) : (
          <>
            <span>
              modelled gross <strong>{formatDecimal(m.expectancy.gross_bps)} bps</strong>
            </span>
            <span class="faint">
              target {formatDecimal(m.expectancy.target_bps)} bps, stop{" "}
              {formatDecimal(m.expectancy.stop_bps)} bps, over {m.expectancy.sample_n}
            </span>
          </>
        )}
      </div>

      <div class="gap-meta">
        {wf === null ? (
          <span class="faint">
            no walk-forward has been run, so nothing has been tested out of sample
          </span>
        ) : (
          <>
            <span>
              walk-forward{" "}
              <strong>{wf.positive ? "held out of sample" : "did not hold"}</strong>
            </span>
            <span class="faint">
              {wf.qualifying_windows} of {wf.windows} windows qualified, pooled n{" "}
              {wf.pooled_n} ({wf.live_pooled_n} live, {wf.backfilled_pooled_n}{" "}
              backfilled), pooled hit rate {formatHitRate(wf.pooled_hit_rate)}
            </span>
          </>
        )}
      </div>
    </li>
  );
}
