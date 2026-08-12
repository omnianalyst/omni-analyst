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

function initialTab(): Tab {
  if (typeof window === "undefined") return "scanner";
  const params = new URLSearchParams(window.location.search);
  const requested = params.get("tab");
  if (requested && TABS.some((tab) => tab.id === requested)) return requested as Tab;
  return params.has("q") ? "search" : "scanner";
}

export function DiscoverView() {
  const [tab, setTab] = useState<Tab>(initialTab);

  function selectTab(next: Tab) {
    setTab(next);
    const url = new URL(window.location.href);
    url.searchParams.set("tab", next);
    if (next !== "search") url.searchParams.delete("q");
    window.history.replaceState({}, "", url);
  }

  return (
    <div class="discover-view">
      <div class="tab-bar" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            class={`tab ${tab === t.id ? "tab-active" : ""}`}
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => selectTab(t.id)}
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
