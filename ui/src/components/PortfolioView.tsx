import { useEffect, useRef, useState } from "preact/hooks";
import { AuthRequiredError } from "../lib/auth";
import { getHoldings } from "../lib/holdings";
import { AddPositionModal, HoldingsTable } from "./ManualHoldings";
import { WalletsModal } from "./WalletsModal";

// The page is two surfaces: your manually tracked positions (valued from
// the system's own coverage) and read-only external wallets. Both actions
// open modals from the header; nothing renders until asked for.
export function PortfolioView() {
  const [auth, setAuth] = useState<"checking" | "required">("checking");
  const [addOpen, setAddOpen] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const addTrigger = useRef<HTMLButtonElement | null>(null);

  // An auth probe only: the holdings fetch itself lives in the table
  // component, which owns its own loading and error states.
  useEffect(() => {
    let cancelled = false;
    void getHoldings().catch((cause) => {
      if (!cancelled && cause instanceof AuthRequiredError) setAuth("required");
    });
    return () => { cancelled = true; };
  }, []);

  if (auth === "required") {
    return (
      <section class="quiet-state">
        <p class="eyebrow">Private portfolio</p>
        <h1>Sign in to see your portfolio</h1>
        <p>Your positions and performance are visible only to your account.</p>
        <a class="btn-primary" href="/login">Sign in</a>
      </section>
    );
  }

  return (
    <div class="portfolio-view product-page">
      <header class="compact-status-heading health-quiet">
        <div class="portfolio-hero-copy">
          <div class="health-title-row">
            <span class="health-orb" aria-hidden="true" />
            <div>
              <h1>Portfolio</h1>
              <p>Track what you hold. The system values it from its own price coverage.</p>
            </div>
          </div>
        </div>
        <div class="portfolio-header-actions">
          <button
            type="button"
            class="btn-secondary compact-button"
            ref={addTrigger}
            onClick={() => setAddOpen(true)}
          >
            Add position
          </button>
          <WalletsModal />
        </div>
      </header>

      <AddPositionModal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onAdded={() => setRefreshKey((key) => key + 1)}
        triggerRef={addTrigger}
      />
      <HoldingsTable refreshKey={refreshKey} />
    </div>
  );
}
