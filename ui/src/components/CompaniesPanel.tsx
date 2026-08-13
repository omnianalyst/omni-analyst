import { useEffect, useState } from "preact/hooks";
import {
  ABSENT,
  describeCoverage,
  getCompanies,
  percent,
  tierRows,
  tone,
  type CompaniesData,
} from "../lib/companies";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

type State =
  | { kind: "loading" }
  | { kind: "ok"; data: CompaniesData }
  | { kind: "error" };

const SHOW_STEP = 15;

/** Individual companies, kept structurally apart from the ETF core.
 *
 * The separation is the point. A price-quality ranker over these same names
 * failed its own test (3 of 9 sectors, median excess CAGR -2.70%), so folding
 * them in beside the diversified categories would lend them a standing the
 * evidence does not support. They are shown because hiding a measurement is its
 * own dishonesty, and framed so that is unmistakable.
 */
export function CompaniesPanel() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [shown, setShown] = useState(SHOW_STEP);

  useEffect(() => {
    let cancelled = false;
    void getCompanies()
      .then((data) => !cancelled && setState({ kind: "ok", data }))
      .catch(() => !cancelled && setState({ kind: "error" }));
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "loading") return <Loading label="Measuring companies…" />;
  if (state.kind === "error") {
    return <ErrorState message="Company rankings are currently unavailable." />;
  }

  const { data } = state;
  if (data.companies.length === 0) {
    return (
      <section class="companies-panel surface-card">
        <div class="section-heading">
          <div>
            <p class="eyebrow">Individual companies</p>
            <h2>Measured separately</h2>
          </div>
        </div>
        <div class="clean-empty">
          <strong>No company has enough stored price history yet</strong>
          <span>{describeCoverage(data.coverage)}</span>
        </div>
      </section>
    );
  }

  const visible = data.companies.slice(0, shown);

  return (
    <section class="companies-panel surface-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Individual companies — measured, not endorsed</p>
          <h2>Ranked on the same axes as the core</h2>
        </div>
        <span class="count-badge">{data.coverage.measured}</span>
      </div>

      <p class="companies-verdict">{data.standing.verdict}</p>

      <div class="companies-census">
        {tierRows(data.risk_census).map(({ tier, count }) => (
          <div key={tier}>
            <span class={`risk-badge risk-badge-${tier}`}>
              {tier === "unrated" ? "Unrated" : tier}
            </span>
            <strong>{count}</strong>
          </div>
        ))}
      </div>

      <p class="settings-lead">{describeCoverage(data.coverage)}</p>

      <div class="responsive-table">
        <table class="data-table">
          <thead>
            <tr>
              <th>Company</th>
              <th>Score</th>
              <th>1 year</th>
              <th>90 days</th>
              <th>Volatility</th>
              <th>Deepest fall</th>
              <th>Risk</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((company) => (
              <tr key={company.symbol}>
                <td>
                  <span class="rank-asset">
                    <strong>{company.symbol}</strong>
                    <small>{company.name}</small>
                  </span>
                </td>
                <td>
                  <strong class="canonical-score">
                    {company.scores.balanced?.toFixed(0) ?? ABSENT}
                  </strong>
                </td>
                <td class={tone(company.return_365d)}>{percent(company.return_365d)}</td>
                <td class={tone(company.return_90d)}>{percent(company.return_90d)}</td>
                <td>{percent(company.volatility)}</td>
                <td class={tone(company.max_drawdown)}>{percent(company.max_drawdown)}</td>
                <td>
                  <span class={`risk-badge risk-badge-${company.risk_tier}`}>
                    {company.risk_tier === "unrated" ? "Unrated" : company.risk_tier}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {shown < data.companies.length ? (
        <button
          type="button"
          class="btn-secondary compact-button"
          onClick={() => setShown((n) => n + SHOW_STEP)}
        >
          Show {Math.min(SHOW_STEP, data.companies.length - shown)} more
          {" "}({data.companies.length - shown} remaining)
        </button>
      ) : null}

      <p class="research-note">
        {data.standing.scope} {data.standing.risk_tier} {data.standing.sharpe} Full
        result in <code>{data.standing.report}</code>.
      </p>
    </section>
  );
}
