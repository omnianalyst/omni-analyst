import { useEffect, useRef, useState } from "preact/hooks";
import { ScannerView } from "./ScannerView";
import { SearchView } from "./SearchView";
import { AlertsView } from "./AlertsView";
import { WatchlistView } from "./WatchlistView";

// The page IS the ranked overview. Its actions -- Saved, Alerts, Ask -- sit
// top-right, the same pattern Portfolio uses; Ask opens the objective page
// as an overlay rather than navigating, so a question never loses the ranked
// context it came from. Deep links (?tab=, ?q=) still route where they
// always did.
function overlayFromUrl(): "search" | "watchlist" | "alerts" | null {
  const params = new URLSearchParams(window.location.search);
  if (params.has("q")) return "search";
  const requested = params.get("tab");
  if (requested === "watchlist") return "watchlist";
  if (requested === "alerts") return "alerts";
  return null;
}

export function DiscoverView() {
  const [overlay, setOverlay] = useState<
    "search" | "watchlist" | "alerts" | null
  >(null);
  const [query, setQuery] = useState<string | null>(null);
  const watchlistTrigger = useRef<HTMLButtonElement | null>(null);
  const alertsTrigger = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    const initial = overlayFromUrl();
    if (initial === "search") {
      setQuery(new URLSearchParams(window.location.search).get("q"));
    }
    if (initial !== null) setOverlay(initial);
    const url = new URL(window.location.href);
    url.search = "";
    window.history.replaceState({}, "", url);
  }, []);

  useEffect(() => {
    if (overlay !== null) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOverlay(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [overlay]);

  function close(focus: "watchlist" | "alerts") {
    setOverlay(null);
    requestAnimationFrame(() => {
      (focus === "watchlist" ? watchlistTrigger : alertsTrigger).current?.focus();
    });
  }

  return (
    <div class="discover-view">
      <div class="discover-overlay-bar">
        <div class="portfolio-header-actions">
          <a
            class="btn-secondary compact-button"
            href="/objective"
            title="Ask the system a question"
          >
            Ask
          </a>
          <button
            type="button"
            class="btn-secondary compact-button"
            ref={watchlistTrigger}
            onClick={() => setOverlay("watchlist")}
          >
            Saved
          </button>
          <button
            type="button"
            class="btn-secondary compact-button"
            ref={alertsTrigger}
            onClick={() => setOverlay("alerts")}
          >
            Alerts
          </button>
        </div>
      </div>

      {overlay === "watchlist" ? (
        <Overlay title="Saved" onClose={() => close("watchlist")}>
          <WatchlistView />
        </Overlay>
      ) : null}
      {overlay === "alerts" ? (
        <Overlay title="Alerts" onClose={() => close("alerts")}>
          <AlertsView />
        </Overlay>
      ) : null}
      {overlay === "search" && query !== null ? (
        <Overlay title="Search" onClose={() => setOverlay(null)}>
          <SearchView initialQuery={query} />
        </Overlay>
      ) : null}

      <ScannerView />
    </div>
  );
}

function Overlay({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: preact.ComponentChildren;
}) {
  return (
    <div
      class="learn-why-overlay"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div class="learn-why-card discover-overlay-card">
        <header>
          <h2>{title}</h2>
          <button type="button" class="learn-why-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>
        <div class="wallets-card-body">{children}</div>
      </div>
    </div>
  );
}
