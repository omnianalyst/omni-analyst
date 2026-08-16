import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError, sendJson } from "../lib/api";
import type { PortfolioHistory } from "./ScannerView";

interface UniverseEntry {
  symbol: string;
  name: string;
  kind?: string;
  median_annual_return?: number | null;
}

interface MixIncome {
  yield_pct: number;
  expense_ratio_pct: number;
  not_covered: string[];
}

interface CompareResponse {
  custom: PortfolioHistory;
  policy: PortfolioHistory;
  income: MixIncome | null;
  income_as_of?: string;
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

// Assemble your own mix from anything measured -- broad assets or individual
// companies -- and set it against the policy portfolio over exactly the same
// window. The arithmetic is the server's -- same annual rebalancing, same
// window rules -- so the comparison is honest by construction: what a
// different mix would have gained, and what it would have cost in risk.
export function CustomCompare({
  universe,
  companies,
}: {
  universe: UniverseEntry[];
  companies: UniverseEntry[];
}) {
  const [positions, setPositions] = useState<Position[]>([]);
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const [result, setResult] = useState<Result>({ kind: "idle" });
  const root = useRef<HTMLDivElement>(null);

  // Close the suggestion dropdown on any click outside the picker.
  useEffect(() => {
    if (query.trim() === "") return;
    const onDocClick = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setQuery("");
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [query]);

  const searchable = useMemo(() => {
    const seen = new Set<string>();
    const all: UniverseEntry[] = [];
    for (const entry of [...universe, ...companies]) {
      if (seen.has(entry.symbol)) continue;
      seen.add(entry.symbol);
      all.push(entry);
    }
    return all;
  }, [universe, companies]);
  const names = useMemo(
    () => new Map(searchable.map((entry) => [entry.symbol, entry.name])),
    [searchable],
  );

  const suggestions = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    return searchable
      .filter(
        (entry) =>
          !positions.some((p) => p.symbol === entry.symbol) &&
          (entry.symbol.toLowerCase().includes(q) || entry.name.toLowerCase().includes(q)),
      )
      .slice(0, 8);
  }, [query, searchable, positions]);

  function add(symbol: string) {
    setPositions((current) =>
      current.some((p) => p.symbol === symbol)
        ? current
        : [...current, { symbol, weight: 1 }],
    );
    setQuery("");
    setHighlight(0);
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
          Pick anything measured -- broad assets or individual companies -- set weights, and
          set it against the portfolio above. Same window, same annual rebalancing: see what
          a different mix would have gained, and what it would have cost in risk.
        </p>
      </div>

      <div class="compare-picker" ref={root}>
        <input
          type="search"
          placeholder="Add an asset -- try VTI, GLD, NVDA, BTC..."
          value={query}
          onInput={(event) => {
            setQuery(event.currentTarget.value);
            setHighlight(0);
          }}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown" && suggestions.length > 0) {
              event.preventDefault();
              setHighlight((h) => Math.min(h + 1, suggestions.length - 1));
            } else if (event.key === "ArrowUp" && suggestions.length > 0) {
              event.preventDefault();
              setHighlight((h) => Math.max(h - 1, 0));
            } else if (event.key === "Enter" && suggestions.length > 0) {
              event.preventDefault();
              add(suggestions[highlight]?.symbol ?? suggestions[0].symbol);
            } else if (event.key === "Escape") {
              setQuery("");
            }
          }}
          aria-label="Add an asset to your mix"
          aria-autocomplete="list"
        />
        {suggestions.length > 0 ? (
          <ul class="compare-suggestions" role="listbox">
            {suggestions.map((entry, index) => (
              <li key={entry.symbol}>
                <button
                  type="button"
                  class={index === highlight ? "suggested" : ""}
                  onMouseEnter={() => setHighlight(index)}
                  onClick={() => add(entry.symbol)}
                >
                  <strong>{entry.symbol}</strong>
                  <small>
                    {entry.name}
                    {entry.kind === "company" ? " · company" : ""}
                  </small>
                  <span class="mono">
                    {entry.median_annual_return != null
                      ? `${entry.median_annual_return.toFixed(1)}% med`
                      : ""}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      {positions.length > 0 ? (
        <ul class="compare-positions">
          {positions.map((position) => (
            <li key={position.symbol}>
              <strong>{position.symbol}</strong>
              <small>{names.get(position.symbol)}</small>
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
          ))}
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
        <>
          <CompareTable custom={result.data.custom} policy={result.data.policy} />
          {result.data.income ? (
            <p class="risk-note">
              Income about {result.data.income.yield_pct.toFixed(1)}%/yr, costing{" "}
              {result.data.income.expense_ratio_pct.toFixed(2)}%/yr in fund fees, over the
              assets with sponsor figures
              {result.data.income.not_covered.length > 0
                ? ` (${result.data.income.not_covered.join(", ")} carry none -- companies and crypto pay no fund yield tracked here)`
                : ""}
              , as of {result.data.income_as_of ?? "last audit"}.
            </p>
          ) : null}
        </>
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
