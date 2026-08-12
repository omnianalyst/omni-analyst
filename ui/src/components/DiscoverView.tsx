import { useState } from "preact/hooks";
import { ScannerView } from "./ScannerView";
import { SearchView } from "./SearchView";
import { WatchlistView } from "./WatchlistView";
import { AlertsView } from "./AlertsView";

type Tab = "scanner" | "search" | "watchlist" | "alerts";

const TABS: { id: Tab; label: string }[] = [
  { id: "scanner", label: "Overview" },
  { id: "search", label: "Search" },
  { id: "watchlist", label: "Saved" },
  { id: "alerts", label: "Alerts" },
];

export function DiscoverView() {
  const [tab, setTab] = useState<Tab>("scanner");

  return (
    <div class="discover-view">
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
        {tab === "scanner" && <ScannerView />}
        {tab === "search" && <SearchView />}
        {tab === "watchlist" && <WatchlistView />}
        {tab === "alerts" && <AlertsView />}
      </div>
    </div>
  );
}
