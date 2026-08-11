import { useState } from "preact/hooks";
import { SearchView } from "./SearchView";
import { WatchlistView } from "./WatchlistView";
import { AlertsView } from "./AlertsView";

type Tab = "search" | "watchlist" | "alerts";

const TABS: { id: Tab; label: string }[] = [
  { id: "search", label: "Search" },
  { id: "watchlist", label: "Watchlists" },
  { id: "alerts", label: "Alerts" },
];

export function DiscoverView() {
  const [tab, setTab] = useState<Tab>("search");

  return (
    <div class="discover-view">
      <header class="page-head">
        <h1>Discover</h1>
        <p class="muted">
          Find entities in the coverage store, track them on watchlists, and
          set alerts on the claim types that matter.
        </p>
      </header>

      <div class="tab-bar" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            class={`tab ${tab === t.id ? "tab-active" : ""}`}
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div class="tab-content" role="tabpanel">
        {tab === "search" && <SearchView />}
        {tab === "watchlist" && <WatchlistView />}
        {tab === "alerts" && <AlertsView />}
      </div>
    </div>
  );
}
