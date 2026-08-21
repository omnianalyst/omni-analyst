import { useState } from "preact/hooks";
import { describeError } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import { addEntity, createWatchlist, listWatchlists } from "../lib/watchlist";

type TrackState =
  | { kind: "idle" }
  | { kind: "adding" }
  | { kind: "done"; watchlistName: string }
  | { kind: "error"; message: string };

// The entity page's one action: put this name on a watchlist, which is the
// demand channel that makes the system keep it covered. Watching is the
// product's whole verb, so it sits in the header, not behind a menu.
//
// Watchlist selection is deliberately simple: the first existing list, or a
// "Watchlist" created on first track. An operator running several lists
// manages membership from the watchlist surface; here the job is one click.
export function TrackButton({ entityId }: { entityId: string }) {
  const [state, setState] = useState<TrackState>({ kind: "idle" });

  async function onTrack() {
    if (state.kind === "adding") return;
    setState({ kind: "adding" });
    try {
      let lists = (await listWatchlists()).watchlists;
      let target = lists[0];
      if (!target) {
        target = await createWatchlist("Watchlist");
      }
      await addEntity(target.id, entityId);
      setState({ kind: "done", watchlistName: target.name });
    } catch (err) {
      if (err instanceof AuthRequiredError) {
        setState({
          kind: "error",
          message: "Sign in to track — watching raises coverage demand.",
        });
        return;
      }
      const { message } = describeError(err);
      setState({ kind: "error", message });
    }
  }

  if (state.kind === "done") {
    return (
      <span class="track-done" role="status">
        Tracking · updates to {state.watchlistName}
      </span>
    );
  }

  return (
    <span class="track-wrap">
      <button
        type="button"
        class="btn-secondary compact-button track-button"
        onClick={() => void onTrack()}
        disabled={state.kind === "adding"}
      >
        {state.kind === "adding" ? "Tracking…" : "Track"}
      </button>
      {state.kind === "error" ? (
        <span class="track-error" role="alert">{state.message}</span>
      ) : null}
    </span>
  );
}
