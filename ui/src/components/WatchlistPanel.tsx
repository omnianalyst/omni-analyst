import { useEffect, useState } from "preact/hooks";
import { describeError, searchEntities, type Entity } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import {
  addEntity,
  entryName,
  entrySymbol,
  listEntries,
  removeEntity,
  type Watchlist,
  type WatchlistEntry,
} from "../lib/watchlist";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

type EntriesState =
  | { kind: "loading" }
  | { kind: "ok"; entries: WatchlistEntry[] }
  | { kind: "error"; message: string; detail?: string };

type SearchState =
  | { kind: "idle" }
  | { kind: "loading"; q: string }
  | { kind: "ok"; q: string; entities: Entity[] }
  | { kind: "empty"; q: string }
  | { kind: "error"; message: string; detail?: string };

const fieldStyle = { display: "grid", gap: "4px" } as const;
const linkBtnStyle = {
  background: "transparent",
  border: "1px solid var(--border-strong)",
  color: "var(--accent)",
  padding: "6px 12px",
  borderRadius: "6px",
  cursor: "pointer",
  font: "inherit",
  fontSize: "13px",
} as const;
const rowInnerStyle = {
  display: "flex",
  alignItems: "center",
  gap: "16px",
  flex: "1 1 auto",
  minWidth: 0,
} as const;
const addRowStyle = {
  display: "flex",
  alignItems: "center",
  gap: "16px",
  padding: "12px 16px",
  background: "var(--panel-2)",
  border: "1px solid var(--border)",
  borderRadius: "8px",
} as const;

export function WatchlistPanel({ watchlist }: { watchlist: Watchlist }) {
  const [entries, setEntries] = useState<EntriesState>({ kind: "loading" });
  const [showAdd, setShowAdd] = useState(false);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState<SearchState>({ kind: "idle" });
  const [addingId, setAddingId] = useState<string | null>(null);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [mutateError, setMutateError] = useState<{
    message: string;
    detail?: string;
  } | null>(null);

  async function refresh() {
    setEntries({ kind: "loading" });
    try {
      const res = await listEntries(watchlist.id);
      setEntries({ kind: "ok", entries: res.entries });
    } catch (err) {
      const { message, detail } = describeErrorAuth(err);
      setEntries({ kind: "error", message, detail });
    }
  }

  useEffect(() => {
    void refresh();
    // refresh closes over a stable watchlist.id; mount-only is intended.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onSearch(e: Event) {
    e.preventDefault();
    const q = query.trim();
    if (!q) {
      setSearch({ kind: "idle" });
      return;
    }
    setSearch({ kind: "loading", q });
    try {
      const res = await searchEntities(q);
      if (res.entities.length === 0) {
        setSearch({ kind: "empty", q });
      } else {
        setSearch({ kind: "ok", q, entities: res.entities });
      }
    } catch (err) {
      const { message, detail } = describeError(err);
      setSearch({ kind: "error", message, detail });
    }
  }

  async function onAdd(entityId: string) {
    setMutateError(null);
    setAddingId(entityId);
    try {
      await addEntity(watchlist.id, entityId);
      await refresh();
      setSearch({ kind: "idle" });
      setQuery("");
    } catch (err) {
      const { message, detail } = describeErrorAuth(err);
      setMutateError({ message, detail });
    } finally {
      setAddingId(null);
    }
  }

  async function onRemove(entityId: string) {
    setMutateError(null);
    setRemovingId(entityId);
    try {
      await removeEntity(watchlist.id, entityId);
      await refresh();
    } catch (err) {
      const { message, detail } = describeErrorAuth(err);
      setMutateError({ message, detail });
    } finally {
      setRemovingId(null);
    }
  }

  return (
    <section class="panel">
      <h2 class="panel-title">
        {watchlist.name}
      </h2>

      {mutateError ? (
        <div style={{ padding: "12px 18px 0" }}>
          <ErrorState message={mutateError.message} detail={mutateError.detail} />
        </div>
      ) : null}

      <div>
        {entries.kind === "loading" ? (
          <Loading label={"Loading entries\u2026"} />
        ) : null}
        {entries.kind === "error" ? (
          <ErrorState message={entries.message} detail={entries.detail} />
        ) : null}
        {entries.kind === "ok" && entries.entries.length === 0 ? (
          <p class="empty">
            Nothing saved here yet. Use Add entity below to find a ticker.
          </p>
        ) : null}
        {entries.kind === "ok" && entries.entries.length > 0 ? (
          <ul class="gaps">
            {entries.entries.map((e) => (
              <li class="gap-row" key={e.entity_id}>
                <div class="gap-head">
                  <a href={`/entity/${e.entity_id}`} style={rowInnerStyle}>
                    <span class="entity-symbol">{entrySymbol(e)}</span>
                    <span class="entity-body">
                      <span class="entity-name">{entryName(e)}</span>
                      <span class="entity-kind">{e.kind}</span>
                    </span>
                  </a>
                  <button
                    style={linkBtnStyle}
                    type="button"
                    disabled={removingId === e.entity_id}
                    onClick={() => void onRemove(e.entity_id)}
                  >
                    {removingId === e.entity_id ? "Removing\u2026" : "Remove"}
                  </button>
                </div>
                {e.added_at ? (
                  <div class="gap-meta">
                    <span class="faint">added {e.added_at.slice(0, 10)}</span>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      <div style={{ borderTop: "1px solid var(--border)", padding: "14px 18px" }}>
        {showAdd ? (
          <form onSubmit={onSearch} style={{ display: "grid", gap: "10px" }}>
            <label style={fieldStyle}>
              <span class="mono">add an entity by symbol or name</span>
              <input
                class="search-input"
                type="text"
                value={query}
                onInput={(e) =>
                  setQuery((e.target as HTMLInputElement).value)
                }
                placeholder="e.g. AAPL"
                aria-label="Search entities to add"
              />
            </label>
            <div style={{ display: "flex", gap: "8px" }}>
              <button
                class="search-btn"
                type="submit"
                disabled={search.kind === "loading"}
              >
                {search.kind === "loading" ? "Searching\u2026" : "Search"}
              </button>
              <button
                style={linkBtnStyle}
                type="button"
                onClick={() => {
                  setShowAdd(false);
                  setSearch({ kind: "idle" });
                  setQuery("");
                }}
              >
                Cancel
              </button>
            </div>

            {search.kind === "empty" ? (
              <p class="empty">
                {`No entities matched "${search.q}". The API answered; nothing matched.`}
              </p>
            ) : null}
            {search.kind === "error" ? (
              <ErrorState message={search.message} detail={search.detail} />
            ) : null}
            {search.kind === "ok" ? (
              <ul class="entity-list">
                {search.entities.map((ent) => {
                  const already =
                    entries.kind === "ok" &&
                    entries.entries.some((x) => x.entity_id === ent.id);
                  return (
                    <li key={ent.id}>
                      <div style={addRowStyle}>
                        <span class="entity-symbol">
                          {ent.symbol ?? "\u2014"}
                        </span>
                        <span class="entity-body">
                          <span class="entity-name">
                            {ent.name ?? "(unnamed)"}
                          </span>
                          <span class="entity-kind">{ent.kind}</span>
                        </span>
                        <button
                          style={linkBtnStyle}
                          type="button"
                          disabled={addingId === ent.id || already}
                          title={
                            already ? "Already on this watchlist" : undefined
                          }
                          onClick={() => void onAdd(ent.id)}
                        >
                          {already
                            ? "On list"
                            : addingId === ent.id
                              ? "Adding\u2026"
                              : "Add"}
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            ) : null}
          </form>
        ) : (
          <button
            style={linkBtnStyle}
            type="button"
            onClick={() => setShowAdd(true)}
          >
            Add entity
          </button>
        )}
      </div>
    </section>
  );
}

function describeErrorAuth(err: unknown): {
  message: string;
  detail?: string;
} {
  if (err instanceof AuthRequiredError) {
    return {
      message: "Authentication required",
      detail: "Your token is missing or no longer valid.",
    };
  }
  return describeError(err);
}
