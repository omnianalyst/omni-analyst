import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import {
  addHolding,
  describeHoldings,
  editHolding,
  getHoldings,
  removeHolding,
  type HoldingsRecord,
  type ManualHolding,
} from "../lib/holdings";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

function money(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: value >= 100 ? 0 : 2,
  });
}

function pnlClass(value: number | null): string {
  if (value === null || value === 0) return "value-flat";
  return value > 0 ? "value-positive" : "value-negative";
}

function AddForm({ onAdded }: { onAdded: () => void }) {
  const [symbol, setSymbol] = useState("");
  const [quantity, setQuantity] = useState("");
  const [basis, setBasis] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: Event) {
    event.preventDefault();
    setError(null);
    if (!symbol.trim() || !quantity.trim()) {
      setError("Symbol and quantity are required.");
      return;
    }
    setBusy(true);
    try {
      await addHolding({
        symbol: symbol.trim().toUpperCase(),
        quantity: quantity.trim(),
        cost_basis: basis.trim() === "" ? undefined : basis.trim(),
      });
      setSymbol("");
      setQuantity("");
      setBasis("");
      onAdded();
    } catch (cause) {
      const described = describeError(cause);
      setError(described.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form class="holding-add" onSubmit={submit}>
      <label>
        Symbol
        <input
          value={symbol}
          onInput={(e) => setSymbol((e.target as HTMLInputElement).value)}
          placeholder="BTC, ETH, SPY…"
          autocomplete="off"
          required
        />
      </label>
      <label>
        Quantity
        <input
          value={quantity}
          onInput={(e) => setQuantity((e.target as HTMLInputElement).value)}
          placeholder="0.5"
          inputMode="decimal"
          required
        />
      </label>
      <label>
        Cost basis (optional)
        <input
          value={basis}
          onInput={(e) => setBasis((e.target as HTMLInputElement).value)}
          placeholder="Total paid"
          inputMode="decimal"
        />
      </label>
      <button type="submit" class="btn-secondary" disabled={busy}>
        {busy ? "Adding…" : "Track position"}
      </button>
      {error ? <p class="inline-warning" role="alert">{error}</p> : null}
    </form>
  );
}

function Row({ holding, onChanged }: { holding: ManualHolding; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [quantity, setQuantity] = useState(String(holding.quantity));
  const [basis, setBasis] = useState(
    holding.cost_basis === null ? "" : String(holding.cost_basis),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await editHolding(holding.id, {
        quantity: quantity.trim(),
        cost_basis: basis.trim() === "" ? undefined : basis.trim(),
      });
      setEditing(false);
      onChanged();
    } catch (cause) {
      setError(describeError(cause).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    try {
      await removeHolding(holding.id);
      onChanged();
    } catch (cause) {
      setError(describeError(cause).message);
      setBusy(false);
    }
  }

  return (
    <tr>
      <td>
        <strong>{holding.symbol}</strong>
        {holding.note ? <small> {holding.note}</small> : null}
      </td>
      <td>
        {editing ? (
          <input
            value={quantity}
            onInput={(e) => setQuantity((e.target as HTMLInputElement).value)}
            inputMode="decimal"
            aria-label={`Quantity of ${holding.symbol}`}
          />
        ) : (
          holding.quantity.toLocaleString()
        )}
      </td>
      <td>
        {holding.valuation === "priced"
          ? money(holding.last_price)
          : <span class="value-flat" title="No price in the store for this symbol yet">unpriced</span>}
      </td>
      <td>{money(holding.value)}</td>
      <td>
        {holding.cost_basis === null ? (
          <span class="value-flat" title="No cost basis recorded">—</span>
        ) : (
          <span class={pnlClass(holding.unrealized_pnl)}>
            {money(holding.unrealized_pnl)}
          </span>
        )}
      </td>
      <td class="holding-actions">
        {editing ? (
          <>
            <button type="button" class="btn-secondary" onClick={() => void save()} disabled={busy}>
              Save
            </button>
            <button type="button" class="btn-secondary" onClick={() => setEditing(false)} disabled={busy}>
              Cancel
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              class="btn-secondary"
              onClick={() => {
                setQuantity(String(holding.quantity));
                setBasis(holding.cost_basis === null ? "" : String(holding.cost_basis));
                setEditing(true);
              }}
            >
              Edit
            </button>
            <button type="button" class="btn-secondary" onClick={() => void remove()} disabled={busy}>
              Remove
            </button>
          </>
        )}
        {error ? <span class="inline-warning" role="alert">{error}</span> : null}
      </td>
    </tr>
  );
}

export function ManualHoldings() {
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "ok"; data: HoldingsRecord } | { kind: "error"; message: string }
  >({ kind: "loading" });

  function load() {
    setState({ kind: "loading" });
    void getHoldings()
      .then((data) => setState({ kind: "ok", data }))
      .catch((cause) => setState({ kind: "error", message: describeError(cause).message }));
  }

  useEffect(load, []);

  if (state.kind === "loading") {
    return (
      <section class="surface-card holdings-card">
        <Loading label="Loading your positions…" />
      </section>
    );
  }
  if (state.kind === "error") {
    return (
      <section class="surface-card holdings-card">
        <div class="section-heading">
          <div><p class="eyebrow">Your positions</p><h2>Positions unavailable</h2></div>
        </div>
        <ErrorState message={state.message} />
      </section>
    );
  }

  const { holdings, summary } = state.data;
  return (
    <section class="surface-card holdings-card" aria-label="Your positions">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Your positions</p>
          <h2>{summary.total_value === null ? "Tracking" : money(summary.total_value)}</h2>
        </div>
        {summary.total_pnl !== null ? (
          <span class={pnlClass(summary.total_pnl)}>{money(summary.total_pnl)} all-time</span>
        ) : null}
      </div>
      <p class="quiet-line">{describeHoldings(summary)}</p>
      <AddForm onAdded={load} />
      {holdings.length > 0 ? (
        <div class="responsive-table">
          <table class="data-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Quantity</th>
                <th>Price</th>
                <th>Value</th>
                <th>P&amp;L</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {holdings.map((holding) => (
                <Row key={holding.id} holding={holding} onChanged={load} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
