import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import { addEntity, createWatchlist, listEntries, listWatchlists, removeEntity } from "../lib/watchlist";

type TrackState =
  | { kind: "checking" }
  | { kind: "untracked"; watchlistId: string }
  | { kind: "adding" }
  | { kind: "tracked"; watchlistId: string; watchlistName: string }
  | { kind: "removing" }
  | { kind: "error"; message: string };

// The entity page's one action: put this name on a watchlist -- or take it
// off. Watching is the demand channel that makes the system keep it covered,
// so the button reflects the current state rather than assuming not-tracked.
// On load it checks the first watchlist (the same one Track adds to); removal
// from a different list happens on the watchlist surface, where lists are the
// object being managed.
export function TrackButton({ entityId }: { entityId: string }) {
  const [state, setState] = useState<TrackState>({ kind: "checking" });

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const lists = (await listWatchlists()).watchlists;
        const first = lists[0];
        if (!first) {
          if (!cancelled) setState({ kind: "untracked", watchlistId: "" });
          return;
        }
        const entries = (await listEntries(first.id)).entries;
        const tracked = entries.some((e) => e.entity_id === entityId);
        if (!cancelled) {
          setState(
            tracked
              ? { kind: "tracked", watchlistId: first.id, watchlistName: first.name }
              : { kind: "untracked", watchlistId: first.id },
          );
        }
      } catch {
        // Anonymous or unreachable: the button still works as a sign-in prompt
        // on click; pre-loading tracked state is an optimization, not a gate.
        if (!cancelled) setState({ kind: "untracked", watchlistId: "" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [entityId]);

  async function onTrack() {
    setState((s) => (s.kind === "untracked" ? { kind: "adding" } : s));
    try {
      let watchlistId = state.kind === "untracked" ? state.watchlistId : "";
      let watchlistName = "";
      if (!watchlistId) {
        const created = await createWatchlist("Watchlist");
        watchlistId = created.id;
        watchlistName = created.name;
      } else {
        const lists = (await listWatchlists()).watchlists;
        watchlistName = lists.find((l) => l.id === watchlistId)?.name ?? "Watchlist";
      }
      await addEntity(watchlistId, entityId);
      setState({ kind: "tracked", watchlistId, watchlistName });
    } catch (err) {
      setState(toError(err));
    }
  }

  async function onUntrack() {
    if (state.kind !== "tracked") return;
    const { watchlistId } = state;
    setState({ kind: "removing" });
    try {
      await removeEntity(watchlistId, entityId);
      setState({ kind: "untracked", watchlistId });
    } catch (err) {
      setState(toError(err));
    }
  }

  if (state.kind === "tracked" || state.kind === "removing") {
    const busy = state.kind === "removing";
    const name = state.kind === "tracked" ? state.watchlistName : "";
    return (
      <span class="track-wrap">
        <button
          type="button"
          class="btn-secondary compact-button track-button"
          onClick={() => void onUntrack()}
          disabled={busy}
          title={name
            ? `On ${name} -- removing withdraws its coverage demand`
            : "Removing…"}
        >
          {busy ? "Removing…" : "Tracking"}
        </button>
      </span>
    );
  }

  return (
    <span class="track-wrap">
      <button
        type="button"
        class="btn-secondary compact-button track-button"
        onClick={() => void onTrack()}
        disabled={state.kind === "adding" || state.kind === "checking"}
      >
        {state.kind === "adding"
          ? "Tracking…"
          : state.kind === "checking"
            ? "…"
            : "Track"}
      </button>
      {state.kind === "error" ? (
        <span class="track-error" role="alert">{state.message}</span>
      ) : null}
    </span>
  );
}

function toError(err: unknown): TrackState {
  if (err instanceof AuthRequiredError) {
    return {
      kind: "error",
      message: "Sign in to track — watching raises coverage demand.",
    };
  }
  return { kind: "error", message: describeError(err).message };
}
