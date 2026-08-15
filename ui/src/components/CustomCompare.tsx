import { useMemo, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError, sendJson } from "../lib/api";
import type { AssetMetric, PortfolioHistory } from "./ScannerView";

interface CompareResponse {
  custom: PortfolioHistory;
  policy: PortfolioHistory;
}

interface Position {
  symbol: string;
  weight: number;
}

type Result =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ok"; data: CompareResponse };

// Assemble your own mix from anything measured and set it against the policy
// portfolio over exactly the same window. The arithmetic is the server's --
// same annual rebalancing, same window rules -- so the comparison is honest
// by construction: what a different mix would have gained, and what it would
// have cost in risk.
export function CustomCompare({ universe }: { universe: AssetMetric[] }) {
  const [positions, setPositions] = useState<Position[]>([]);
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<Result>({ kind: "idle" });

  const bySymbol = useMemo(
    () => new Map(universe.map((asset) => [asset.symbol, asset])),
    [universe],
  );
  const suggestions = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    return universe
      .filter(
        (asset) =>
          !positions.some((p) => p.symbol === asset.symbol) &&
          (asset.symbol.toLowerCase().includes(q) || asset.name.toLowerCase().includes(q)),
      )
      .slice(0, 6);
  }, [query, universe, positions]);

  function add(symbol: string) {
    setPositions((current) =>
      current.some((p) => p.symbol === symbol)
        ? current
        : [...current, { symbol, weight: 1 }],
    );
    setQuery("");
    setResult({ kind: "idle" });
  }

  function remove(symbol: string) {
    setPositions((current) => current.filter((p) => p.symbol !== symbol));
    setResult({ kind: "idle" });
  }

  function setWeight(symbol: string, weight: number) {
    setPositions((current) =>
      current.map((p) => (p.symbol === symbol ? { ...p, weight: Math.max(weight, 0.01) } : p)),
    );
    setResult({ kind: "idle" });
  }

  async function compare() {
    setResult({ kind: "loading" });
    try {
      const data = await sendJson<CompareResponse>(
        "POST",
        "/scanner/custom-portfolio",
        { positions },
        authHeaderIfPresent(),
      );
      setResult({ kind: "ok", data });
    } catch (error) {
      setResult({ kind: "error", message: describeError(error).message });
    }
  }

  const totalWeight = positions.reduce((sum, p) => sum + p.weight, 0);

  return (
    <section class="custom-compare" aria-label="Compare your own mix">
      <div class="top-picks-heading">
        <h2>Test your own mix</h2>
        <p>
          Pick anything measured, set weights, and set it against the portfolio above -- the
          same window, the same annual rebalancing. See what a different mix would have gained,
          and what it would have cost in risk.
        </p>
      </div>

      <div class="compare-picker">
        <input
          type="search"
          placeholder="Add an asset -- try VTI, GLD, QQQ, BTC..."
          value={query}
          onInput={(event) => setQuery(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && suggestions.length > 0) add(suggestions[0].symbol);
          }}
          aria-label="Add an asset to your mix"
        />
        {suggestions.length > 0 ? (
          <ul class="compare-suggestions">
            {suggestions.map((asset) => (
              <li key={asset.symbol}>
                <button type="button" onClick={() => add(asset.symbol)}>
                  <strong>{asset.symbol}</strong>
                  <small>{asset.name}</small>
                  <span class="mono">
                    {asset.median_annual_return != null ? `${asset.median_annual_return.toFixed(1)}% med` : ""}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      {positions.length > 0 ? (
        <ul class="compare-positions">
          {positions.map((position) => {
            const asset = bySymbol.get(position.symbol);
            return (
              <li key={position.symbol}>
                <strong>{position.symbol}</strong>
                <small>{asset?.name}</small>
                <label>
                  weight
                  <input
                    type="number"
                    min="0.01"
                    step="0.25"
                    value={position.weight}
                    onInput={(event) => setWeight(position.symbol, Number(event.currentTarget.value))}
                  />
                </label>
                <span class="mono">
                  {((position.weight / totalWeight) * 100).toFixed(0)}%
                </span>
                <button type="button" aria-label={`Remove ${position.symbol}`} onClick={() => remove(position.symbol)}>
                  ×
                </button>
              </li>
            );
          })}
        </ul>
      ) : (
        <p class="quiet-line">Nothing added yet. Two or more assets make a mix.</p>
      )}

      <div class="compare-actions">
        <button
          type="button"
          class="btn-primary compact-button"
          disabled={positions.length < 2 || result.kind === "loading"}
          onClick={() => void compare()}
        >
          {result.kind === "loading" ? "Measuring..." : "Compare with the portfolio"}
        </button>
      </div>

      {result.kind === "error" ? <p class="inline-warning">{result.message}</p> : null}

      {result.kind === "ok" ? (
        <CompareTable custom={result.data.custom} policy={result.data.policy} />
      ) : null}
    </section>
  );
}

function fmt(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}`;
}

// Direction a reader hopes for, per row: higher median is better, lower
// volatility is better, shallower falls are better.
const BETTER: Record<string, "higher" | "lower"> = {
  median_year: "higher",
  volatility: "lower",
  "worst_year.return": "higher",
  worst_drawdown: "higher",
  up_years: "higher",
};

function rowValue(history: PortfolioHistory, key: string): number | null {
  if (key === "worst_year.return") return history.worst_year?.return ?? null;
  const value = (history as unknown as Record<string, number>)[key];
  return typeof value === "number" ? value : null;
}

function CompareTable({ custom, policy }: { custom: PortfolioHistory; policy: PortfolioHistory }) {
  const rows: Array<{ key: string; label: string }> = [
    { key: "median_year", label: "Median year" },
    { key: "volatility", label: "Volatility" },
    { key: "worst_year.return", label: "Worst year" },
    { key: "worst_drawdown", label: "Worst fall" },
    { key: "up_years", label: "Up years" },
  ];
  const sameWindow =
    custom.window_start === policy.window_start && custom.window_end === policy.window_end;

  return (
    <div class="compare-result">
      <p class="metric-kicker">
        Measured {custom.window_start} to {custom.window_end} · {custom.complete_years} complete
        years · annual rebalancing{sameWindow ? "" : " (windows differ -- unusual)"} · history,
        not a forecast
      </p>
      <div class="responsive-table">
        <table class="data-table compare-table">
          <thead>
            <tr>
              <th />
              <th>Your mix</th>
              <th>The portfolio</th>
              <th>Difference</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ key, label }) => {
              const mine = rowValue(custom, key);
              const theirs = rowValue(policy, key);
              const delta = mine != null && theirs != null ? mine - theirs : null;
              const better =
                delta == null || delta === 0
                  ? ""
                  : (BETTER[key] === "higher" ? delta > 0 : delta < 0)
                    ? "value-positive"
                    : "value-negative";
              const unit = key === "up_years" ? "%" : "%";
              return (
                <tr key={key}>
                  <td><strong>{label}</strong></td>
                  <td class="mono">
                    {fmt(mine)}
                    {unit === "%" ? "%" : ""}
                    {key === "worst_year" && custom.worst_year ? (
                      <small class="unit-note"> {custom.worst_year.year}</small>
                    ) : null}
                  </td>
                  <td class="mono">
                    {fmt(theirs)}%
                    {key === "worst_year" && policy.worst_year ? (
                      <small class="unit-note"> {policy.worst_year.year}</small>
                    ) : null}
                  </td>
                  <td class={`mono ${better}`}>{delta == null ? "—" : `${delta > 0 ? "+" : ""}${delta.toFixed(1)}`}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p class="risk-note">
        Same measured window for both mixes, so the comparison is like for like. A higher median
        bought with deeper falls is a trade, not a win -- decide which side of it you want to
        sleep on.
      </p>
    </div>
  );
}
