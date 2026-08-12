import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import {
  createWatchlist,
  listWatchlists,
  type Watchlist,
} from "../lib/watchlist";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";
import { WatchlistPanel } from "./WatchlistPanel";

type State =
  | { kind: "loading" }
  | { kind: "auth" }
  | { kind: "ok"; watchlists: Watchlist[] }
  | { kind: "error"; message: string; detail?: string };

export function WatchlistView() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  async function reload() {
    setState({ kind: "loading" });
    try {
      const res = await listWatchlists();
      setState({ kind: "ok", watchlists: res.watchlists });
    } catch (err) {
      if (err instanceof AuthRequiredError) {
        setState({ kind: "auth" });
      } else {
        const { message, detail } = describeError(err);
        setState({ kind: "error", message, detail });
      }
    }
  }

  useEffect(() => {
    void reload();
  }, []);

  async function onCreate(e: Event) {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || creating) return;
    setCreateError(null);
    setCreating(true);
    try {
      await createWatchlist(trimmed);
      setName("");
      await reload();
    } catch (err) {
      setCreateError(
        err instanceof AuthRequiredError
          ? "Authentication required."
          : describeError(err).message,
      );
    } finally {
      setCreating(false);
    }
  }

  return (
    <div class="watchlist-view discover-subview">
      <header class="discover-subview-heading">
        <h1>Saved</h1>
        <p>Keep important entities covered and easy to return to.</p>
      </header>

      {state.kind === "loading" ? (
        <Loading label={"Loading watchlists\u2026"} />
      ) : null}
      {state.kind === "auth" ? <AuthRequired /> : null}
      {state.kind === "error" ? (
        <ErrorState message={state.message} detail={state.detail} />
      ) : null}

      {state.kind === "ok" ? (
        <>
          <section class="watchlist-create-card">
            <div>
              <h2>New list</h2>
              <p>Group companies, assets, or indicators around a question.</p>
            </div>
            <form class="watchlist-create-form" onSubmit={onCreate}>
              <input
                class="watchlist-name-input"
                type="text"
                placeholder="e.g. Mega-cap tech, Macro indicators"
                value={name}
                onInput={(e) => setName((e.target as HTMLInputElement).value)}
                aria-label="Watchlist name"
              />
              {createError ? <p class="auth-error">{createError}</p> : null}
              <button class="search-btn" type="submit" disabled={creating}>
                {creating ? "Creating\u2026" : "Create watchlist"}
              </button>
            </form>
          </section>

          {state.watchlists.length === 0 ? (
            <p class="empty">
              You have no watchlists. The API answered and the list is genuinely
              empty &mdash; an empty list here is the truth, not a loaded view
              that forgot to render.
            </p>
          ) : null}
          {state.watchlists.map((w) => (
            <WatchlistPanel key={w.id} watchlist={w} />
          ))}
        </>
      ) : null}
    </div>
  );
}

function AuthRequired() {
  return (
    <section class="panel">
      <h2 class="panel-title">Sign in to use watchlists</h2>
      <div style={{ padding: "18px" }}>
        <p>
          A watchlist is private to its owner. A request without a verified
          token is refused with 401, and rendering an empty list here would
          pretend the server answered with your data when it did not.
        </p>
        <p style={{ marginTop: "12px" }}>
          <a class="search-btn" href="/login" style={{ textDecoration: "none" }}>
            Sign in
          </a>
        </p>
      </div>
    </section>
  );
}
