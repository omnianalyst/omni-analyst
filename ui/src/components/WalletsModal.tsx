import { useEffect, useRef, useState } from "preact/hooks";
import { WalletAccounts } from "./WalletAccounts";

// The wallets surface as one button + modal overlay. The full component
// (connect buttons, manual add, balances, remove) renders inside unchanged.
// The page composes the button wherever it wants; the modal is self-managed.
export function WalletsModal() {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  function close() {
    setOpen(false);
    requestAnimationFrame(() => trigger.current?.focus());
  }

  return (
    <>
      <button
        type="button"
        class="btn-secondary compact-button"
        ref={trigger}
        onClick={() => setOpen(true)}
      >
        Add wallet
      </button>
      {open ? (
        <div
          class="learn-why-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="External wallets"
          onClick={(event) => {
            if (event.target === event.currentTarget) close();
          }}
        >
          <div class="learn-why-card wallets-card">
            <header>
              <div>
                <h2>External wallets</h2>
                <p class="muted">
                  Read-only public balances. Watched, never traded, never part
                  of any trading NAV.
                </p>
              </div>
              <button type="button" class="learn-why-close" onClick={close} aria-label="Close">
                ×
              </button>
            </header>
            <div class="wallets-card-body">
              <WalletAccounts />
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
