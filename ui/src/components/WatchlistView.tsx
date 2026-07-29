import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import { listWatchlists, type Watchlist } from "../lib/watchlist";
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

  useEffect(() => {
    let cancelled = false;
    setState({ kind: "loading" });
    void (async () => {
      try {
        const res = await listWatchlists();
        if (!cancelled) setState({ kind: "ok", watchlists: res.watchlists });
      } catch (err) {
        if (cancelled) return;
        if (err instanceof AuthRequiredError) {
          setState({ kind: "auth" });
        } else {
          const { message, detail } = describeError(err);
          setState({ kind: "error", message, detail });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div class="watchlist-view">
      <header class="page-head">
        <h1>Watchlists</h1>
        <p class="muted">
          A watchlist is how demand enters the system. Entities on a list are
          kept covered; removing one drops that demand.
        </p>
      </header>

      {state.kind === "loading" ? (
        <Loading label={"Loading watchlists\u2026"} />
      ) : null}
      {state.kind === "auth" ? <AuthRequired /> : null}
      {state.kind === "error" ? (
        <ErrorState message={state.message} detail={state.detail} />
      ) : null}
      {state.kind === "ok" && state.watchlists.length === 0 ? (
        <p class="empty">
          You have no watchlists. The API answered and the list is genuinely
          empty &mdash; an empty list here is the truth, not a loaded view that
          forgot to render.
        </p>
      ) : null}
      {state.kind === "ok" && state.watchlists.length > 0
        ? state.watchlists.map((w) => (
            <WatchlistPanel key={w.id} watchlist={w} />
          ))
        : null}
    </div>
  );
}

function AuthRequired() {
  return (
    <section class="panel">
      <h2 class="panel-title">Authentication required</h2>
      <div style={{ padding: "18px" }}>
        <p>
          A watchlist is private to its owner. A request without a verified
          token is refused with 401, and rendering an empty list here would
          pretend the server answered with your data when it did not.
        </p>
        <p class="mono" style={{ marginTop: "12px" }}>
          {`Set a token to test: localStorage.setItem("omni.auth.token", "<jwt>")`}
        </p>
      </div>
    </section>
  );
}
