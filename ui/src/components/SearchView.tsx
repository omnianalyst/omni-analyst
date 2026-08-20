import { useEffect, useState } from "preact/hooks";
import {
  ApiHttpError,
  ApiUnavailableError,
  describeError,
  searchEntities,
  type Entity,
} from "../lib/api";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

type State =
  | { kind: "idle" }
  | { kind: "loading"; q: string }
  | { kind: "ok"; q: string; entities: Entity[] }
  | { kind: "empty"; q: string }
  | { kind: "error"; message: string; detail?: string };

export function SearchView({ initialQuery }: { initialQuery?: string }) {
  const [input, setInput] = useState(initialQuery ?? "");
  const [state, setState] = useState<State>(
    initialQuery && initialQuery.trim() ? { kind: "loading", q: initialQuery } : { kind: "idle" },
  );

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
    void runSearch(input);
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
          <p class="empty">
            No tickers or companies matched &ldquo;{state.q}&rdquo;.
          </p>
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
