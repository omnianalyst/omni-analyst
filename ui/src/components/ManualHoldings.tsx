import type * as preact from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
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
  const [note, setNote] = useState("");
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
        note: note.trim() === "" ? undefined : note.trim(),
      });
      setSymbol("");
      setQuantity("");
      setBasis("");
      setNote("");
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
      <label>
        Note (optional)
        <input
          value={note}
          onInput={(e) => setNote((e.target as HTMLInputElement).value)}
          placeholder="Why you hold it"
          maxlength={200}
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
  const [note, setNote] = useState(holding.note ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await editHolding(holding.id, {
        quantity: quantity.trim(),
        cost_basis: basis.trim() === "" ? undefined : basis.trim(),
        note: note.trim() === "" ? undefined : note.trim(),
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
        {editing ? (
          <input
            value={note}
            onInput={(e) => setNote((e.target as HTMLInputElement).value)}
            placeholder="Note"
            maxlength={200}
            aria-label={`Note for ${holding.symbol}`}
          />
        ) : (
          <>
            <strong>{holding.symbol}</strong>
            {holding.note ? <small> {holding.note}</small> : null}
          </>
        )}
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
                setNote(holding.note ?? "");
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

// The table half: fetches holdings, renders summary and rows. `refreshKey`
// lets the page force a reload after the add-modal writes one.
export function HoldingsTable({ refreshKey }: { refreshKey: number }) {
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "ok"; data: HoldingsRecord } | { kind: "error"; message: string }
  >({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ kind: "loading" });
    void getHoldings()
      .then((data) => { if (!cancelled) setState({ kind: "ok", data }); })
      .catch((cause) => {
        if (!cancelled) setState({ kind: "error", message: describeError(cause).message });
      });
    return () => { cancelled = true; };
  }, [refreshKey]);

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

  function reload() {
    setState({ kind: "loading" });
    void getHoldings()
      .then((data) => setState({ kind: "ok", data }))
      .catch((cause) => setState({ kind: "error", message: describeError(cause).message }));
  }

  const { holdings, summary } = state.data;
  return (
    <section class="surface-card holdings-card" aria-label="Your positions">
      <div class="section-heading">
        <div>
          <p class="eyebrow">Your positions</p>
          <h2>{summary.total_value === null ? (holdings.length ? "Tracking" : "Nothing tracked yet") : money(summary.total_value)}</h2>
        </div>
        {summary.total_pnl !== null ? (
          <span class={pnlClass(summary.total_pnl)}>{money(summary.total_pnl)} all-time</span>
        ) : null}
      </div>

      <p class="quiet-line">{describeHoldings(summary)}</p>

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
                <Row key={holding.id} holding={holding} onChanged={reload} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

// The modal half: the page owns open state and the trigger button; this is
// the overlay itself. Escape and overlay-click close; focus returns to the
// trigger element the page passes.
export function AddPositionModal({
  open,
  onClose,
  onAdded,
  triggerRef,
}: {
  open: boolean;
  onClose: () => void;
  onAdded: () => void;
  triggerRef: preact.RefObject<HTMLButtonElement | null>;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  function close() {
    onClose();
    requestAnimationFrame(() => triggerRef.current?.focus());
  }

  if (!open) return null;
  return (
    <div
      class="learn-why-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="Add a position"
      onClick={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div class="learn-why-card holdings-add-card">
        <header>
          <div>
            <h2>Track a position</h2>
            <p class="muted">
              Symbol and quantity. Optional total cost basis enables P&amp;L.
              Priced from the system's own coverage, never estimated.
            </p>
          </div>
          <button type="button" class="learn-why-close" onClick={close} aria-label="Close">
            ×
          </button>
        </header>
        <div class="wallets-card-body">
          <AddForm
            onAdded={() => {
              onAdded();
              close();
            }}
          />
        </div>
      </div>
    </div>
  );
}
