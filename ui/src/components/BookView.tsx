import { useState } from "preact/hooks";
import { TradingView } from "./TradingView";
import { ExposureView } from "./ExposureView";

type Tab = "portfolio" | "exposure";

const TABS: { id: Tab; label: string }[] = [
  { id: "portfolio", label: "Portfolio" },
  { id: "exposure", label: "Exposure" },
];

export function BookView() {
  const [tab, setTab] = useState<Tab>("portfolio");

  return (
    <div class="book-view">
      <header class="page-head">
        <h1>Book</h1>
        <p class="muted">
          What the book holds, whether the venues agree, and what the ETF
          holdings actually contain beneath the surface.
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
        {tab === "portfolio" ? <TradingView /> : <ExposureView />}
      </div>
    </div>
  );
}
