import { useEffect, useState } from "preact/hooks";
import {
  ApiHttpError,
  ApiUnavailableError,
  CREATABLE_KINDS,
  createEntity,
  describeError,
  searchEntities,
  type CreatableKind,
  type Entity,
} from "../lib/api";
import { getAuthToken } from "../lib/auth";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

const KIND_LABELS: Record<CreatableKind, string> = {
  company: "Stock",
  etf: "ETF",
  crypto_asset: "Crypto",
};

type State =
  | { kind: "idle" }
  | { kind: "loading"; q: string }
  | { kind: "ok"; q: string; entities: Entity[] }
  | { kind: "empty"; q: string }
  | { kind: "error"; message: string; detail?: string };

type CreateState =
  | { kind: "idle" }
  | { kind: "creating" }
  | { kind: "error"; message: string };

export function SearchView({ initialQuery }: { initialQuery?: string }) {
  const [input, setInput] = useState(initialQuery ?? "");
  const [state, setState] = useState<State>(
    initialQuery && initialQuery.trim() ? { kind: "loading", q: initialQuery } : { kind: "idle" },
  );
  const [createKind, setCreateKind] = useState<CreatableKind>("etf");
  const [createState, setCreateState] = useState<CreateState>({ kind: "idle" });

  useEffect(() => {
    if (initialQuery && initialQuery.trim()) {
      void runSearch(initialQuery);
    }
    // runSearch is stable enough for mount-only; disabling exhaustive-deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function runSearch(raw: string) {
    const q = raw.trim();
    if (!q) {
      setState({ kind: "idle" });
      return;
    }
    setState({ kind: "loading", q });
    const url = new URL(window.location.href);
    url.searchParams.set("tab", "search");
    url.searchParams.set("q", q);
    window.history.replaceState({}, "", url);
    try {
      const res = await searchEntities(q);
      if (res.entities.length === 0) {
        setState({ kind: "empty", q });
      } else {
        setState({ kind: "ok", q, entities: res.entities });
      }
    } catch (err) {
      const { message, detail } = describeError(err);
      setState({ kind: "error", message, detail });
    }
  }

  function onSubmit(e: Event) {
    e.preventDefault();
    setCreateState({ kind: "idle" });
    void runSearch(input);
  }

  async function onCreate(raw: string) {
    const symbol = raw.trim();
    if (!symbol) return;
    setCreateState({ kind: "creating" });
    try {
      const entity = await createEntity(symbol, createKind);
      window.location.assign(`/entity/${entity.id}`);
    } catch (err) {
      if (err instanceof ApiHttpError && err.status === 401) {
        setCreateState({
          kind: "error",
          message: "Sign in to track a new ticker — attention needs an owner.",
        });
        return;
      }
      const { message } = describeError(err);
      setCreateState({ kind: "error", message });
    }
  }

  return (
    <div class="search-view discover-subview">
      <header class="discover-subview-heading">
        <h1>Search</h1>
        <p>Find a stock, crypto asset, ETF, or economic series.</p>
      </header>

      <form class="search-form" onSubmit={onSubmit} role="search">
        <input
          class="search-input"
          type="text"
          name="q"
          value={input}
          onInput={(e) => setInput((e.target as HTMLInputElement).value)}
          placeholder="e.g. AAPL, GDP, Treasuries…"
          autocomplete="off"
          aria-label="Search entities"
        />
        <button class="search-btn" type="submit">
          Search
        </button>
      </form>

      <section class="results" aria-live="polite">
        {state.kind === "idle" && (
          <p class="empty">
            Enter a symbol or name to inspect its available measurements.
          </p>
        )}
        {state.kind === "loading" && (
          <Loading label={`Searching \u201c${state.q}\u201d\u2026`} />
        )}
        {state.kind === "empty" && (
          <div class="search-empty">
            <p class="empty">
              Nothing in the covered universe matches &ldquo;{state.q}&rdquo;.
            </p>
            <div class="track-offer surface-card">
              <p>
                The system covers what someone asks for. If {state.q.trim().toUpperCase()} is a
                real listed ticker, tracking it creates the entity and starts
                collecting &mdash; no data is invented, and if no provider
                recognizes it the gap stays visibly unfilled.
              </p>
              {getAuthToken() ? (
                <div class="track-offer-row">
                  <label class="visually-hidden" for="track-kind">Kind</label>
                  <select
                    id="track-kind"
                    class="kind-select"
                    value={createKind}
                    onChange={(e) =>
                      setCreateKind((e.target as HTMLSelectElement).value as CreatableKind)}
                  >
                    {CREATABLE_KINDS.map((k) => (
                      <option value={k}>{KIND_LABELS[k]}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    class="btn-primary"
                    disabled={createState.kind === "creating"}
                    onClick={() => void onCreate(state.q)}
                  >
                    {createState.kind === "creating"
                      ? "Creating…"
                      : `Track ${state.q.trim().toUpperCase()}`}
                  </button>
                </div>
              ) : (
                <p class="quiet-line">Sign in to track a new ticker.</p>
              )}
              {createState.kind === "error" && (
                <p class="inline-warning">{createState.message}</p>
              )}
            </div>
          </div>
        )}
        {state.kind === "error" && (
          <ErrorState message={state.message} detail={state.detail} />
        )}
        {state.kind === "ok" && (
          <ul class="entity-list">
            {state.entities.map((ent) => (
              <li key={ent.id}>
                <a class="entity-card" href={`/entity/${ent.id}`}>
                  <span class="entity-symbol">
                    {ent.symbol ?? "\u2014"}
                  </span>
                  <span class="entity-body">
                    <span class="entity-name">{ent.name ?? "(unnamed)"}</span>
                    <span class="entity-kind">{ent.kind}</span>
                  </span>
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
