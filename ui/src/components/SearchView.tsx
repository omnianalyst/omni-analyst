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

export function SearchView() {
  const [input, setInput] = useState("");
  const [state, setState] = useState<State>({ kind: "idle" });

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const q = params.get("q");
    if (q && q.trim()) {
      setInput(q);
      void runSearch(q);
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
    <div class="search-view">
      <header class="page-head">
        <h1>Coverage search</h1>
        <p class="muted">
          Find an entity by symbol or name. Empty search shows nothing — a
          network that looks full is worse than one that is honestly empty.
        </p>
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
            Type a symbol or name to search. An empty network is honest; we
            won&apos;t list everything just to look busy.
          </p>
        )}
        {state.kind === "loading" && (
          <Loading label={`Searching \u201c${state.q}\u201d\u2026`} />
        )}
        {state.kind === "empty" && (
          <p class="empty">
            No entities matched “{state.q}”. The API answered; nothing
            matched.
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
