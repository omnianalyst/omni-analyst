import { useEffect, useState } from "preact/hooks";
import { ApiHttpError, describeError } from "../lib/api";
import { connectMetaMask, connectPhantom, type DiscoveredAddress } from "../lib/walletConnect";
import {
  addWallet,
  familyName,
  getWallets,
  refreshWallet,
  refreshWallets,
  removeWallet,
  shortAddress,
  sourceName,
  type WalletAccount,
  type WalletFamily,
  type WalletSource,
} from "../lib/wallets";

type AddMode = "closed" | "ledger" | "manual";

function readableError(error: unknown): string {
  if (error instanceof Error && !(error instanceof ApiHttpError)) return error.message;
  return describeError(error).message;
}

function formatBalance(account: WalletAccount): string {
  const assets = account.balance?.assets ?? [];
  if (!assets.length) return account.refreshed_at ? "No balance found" : "Not refreshed yet";
  return assets
    .slice(0, 3)
    .map((asset) => `${Number(asset.amount).toLocaleString(undefined, { maximumSignificantDigits: 8 })} ${asset.symbol.length > 14 ? `${asset.symbol.slice(0, 6)}…` : asset.symbol}`)
    .join(" · ");
}

function AccountRow({
  account,
  busy,
  onRefresh,
  onRemove,
}: {
  account: WalletAccount;
  busy: boolean;
  onRefresh(): void;
  onRemove(): void;
}) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(account.address);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };
  return (
    <article class="wallet-account-row">
      <div class={`wallet-source-mark wallet-source-${account.source}`} aria-hidden="true">
        {sourceName(account.source).slice(0, 1)}
      </div>
      <div class="wallet-account-main">
        <div class="wallet-account-title">
          <strong>{account.label}</strong>
          <span>{sourceName(account.source)} · {familyName(account.address_family)}</span>
        </div>
        <button class="wallet-address" type="button" title="Copy public address" onClick={() => void copy()}>
          {shortAddress(account.address)} <span>{copied ? "Copied" : "Copy"}</span>
        </button>
      </div>
      <div class="wallet-balance">
        <strong>{formatBalance(account)}</strong>
        <span>{account.refresh_error || account.balance?.coverage || "Public balance only"}</span>
      </div>
      <div class="wallet-row-actions">
        <button type="button" disabled={busy} onClick={onRefresh}>Refresh</button>
        <button class="wallet-remove" type="button" disabled={busy} onClick={onRemove}>Remove</button>
      </div>
    </article>
  );
}

export function WalletAccounts() {
  const [accounts, setAccounts] = useState<WalletAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [addMode, setAddMode] = useState<AddMode>("closed");
  const [family, setFamily] = useState<WalletFamily>("evm");
  const [address, setAddress] = useState("");
  const [label, setLabel] = useState("");

  useEffect(() => {
    let cancelled = false;
    void getWallets()
      .then((response) => {
        if (!cancelled) setAccounts(response.accounts);
      })
      .catch((error) => {
        if (!cancelled) setMessage(readableError(error));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const merge = (account: WalletAccount) => {
    setAccounts((current) => {
      const exists = current.some((item) => item.id === account.id);
      return exists ? current.map((item) => item.id === account.id ? account : item) : [...current, account];
    });
  };

  const addDiscovered = async (
    source: "phantom" | "metamask",
    discovered: DiscoveredAddress[],
  ) => {
    let added = 0;
    let duplicates = 0;
    for (const item of discovered) {
      try {
        const account = await addWallet({
          address_family: item.family,
          address: item.address,
          source,
          label: `${sourceName(source)} ${familyName(item.family)}`,
          discovered_by: "browser_extension",
        });
        merge(await refreshWallet(account.id));
        added += 1;
      } catch (error) {
        if (error instanceof ApiHttpError && error.status === 409) duplicates += 1;
        else throw error;
      }
    }
    if (!added && duplicates) setMessage("Those public accounts are already tracked.");
    else setMessage(`${added} public account${added === 1 ? "" : "s"} connected${duplicates ? `; ${duplicates} already tracked` : ""}.`);
  };

  const connect = async (source: "phantom" | "metamask") => {
    setBusy(source);
    setMessage(null);
    try {
      const discovered = source === "phantom" ? await connectPhantom() : await connectMetaMask();
      await addDiscovered(source, discovered);
    } catch (error) {
      setMessage(readableError(error));
    } finally {
      setBusy(null);
    }
  };

  const submitManual = async (event: Event) => {
    event.preventDefault();
    if (addMode === "closed") return;
    setBusy("add");
    setMessage(null);
    try {
      const source: WalletSource = addMode === "ledger" ? "ledger" : "manual";
      const account = await addWallet({
        address_family: family,
        address,
        source,
        label: label.trim() || `${sourceName(source)} ${familyName(family)}`,
        discovered_by: "manual",
      });
      merge(await refreshWallet(account.id));
      setAddress("");
      setLabel("");
      setAddMode("closed");
      setMessage("Public address added and refreshed.");
    } catch (error) {
      setMessage(readableError(error));
    } finally {
      setBusy(null);
    }
  };

  const refreshOne = async (account: WalletAccount) => {
    setBusy(account.id);
    setMessage(null);
    try {
      merge(await refreshWallet(account.id));
    } catch (error) {
      setMessage(readableError(error));
    } finally {
      setBusy(null);
    }
  };

  const refreshAll = async () => {
    setBusy("refresh-all");
    setMessage(null);
    try {
      const response = await refreshWallets();
      setAccounts((current) => current.map((account) =>
        response.accounts.find((item) => item.id === account.id) ?? account));
    } catch (error) {
      setMessage(readableError(error));
    } finally {
      setBusy(null);
    }
  };

  const remove = async (account: WalletAccount) => {
    if (!window.confirm(`Stop tracking ${account.label}? This does not affect the wallet itself.`)) return;
    setBusy(account.id);
    try {
      await removeWallet(account.id);
      setAccounts((current) => current.filter((item) => item.id !== account.id));
    } catch (error) {
      setMessage(readableError(error));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section class="surface-card wallet-accounts-card">
      <div class="wallet-heading">
        <div>
          <p class="eyebrow">External wallets</p>
          <h2>Wallet balances</h2>
          <p>Read-only public balances. These are not included in trading NAV.</p>
        </div>
        <div class="wallet-connect-actions">
          <button type="button" disabled={busy !== null} onClick={() => void connect("phantom")}>Connect Phantom</button>
          <button type="button" disabled={busy !== null} onClick={() => void connect("metamask")}>Connect MetaMask</button>
          <button type="button" disabled={busy !== null} onClick={() => setAddMode("ledger")}>Add Ledger</button>
          <button type="button" disabled={busy !== null || !accounts.length} onClick={() => void refreshAll()}>Refresh all</button>
        </div>
      </div>

      {message ? <p class="wallet-message" role="status">{message}</p> : null}

      {addMode !== "closed" ? (
        <form class="wallet-add-form" onSubmit={(event) => void submitManual(event)}>
          <div class="wallet-add-copy">
            <strong>{addMode === "ledger" ? "Add a Ledger account" : "Track a public address"}</strong>
            <span>{addMode === "ledger"
              ? "In Ledger Live, choose Receive, copy the address, and confirm that exact address on your Nano screen."
              : "Only enter a public receive address. Never enter a seed phrase or private key."}</span>
          </div>
          <label>
            Network
            <select value={family} onChange={(event) => setFamily(event.currentTarget.value as WalletFamily)}>
              <option value="evm">Ethereum</option>
              <option value="solana">Solana</option>
              <option value="bitcoin">Bitcoin</option>
            </select>
          </label>
          <label class="wallet-address-field">
            Public receive address
            <input required value={address} onInput={(event) => setAddress(event.currentTarget.value)} autocomplete="off" spellcheck={false} />
          </label>
          <label>
            Label (optional)
            <input value={label} maxlength={80} onInput={(event) => setLabel(event.currentTarget.value)} placeholder="Nano X" />
          </label>
          <div class="wallet-form-actions">
            <button type="button" onClick={() => setAddMode("closed")}>Cancel</button>
            <button class="btn-primary" type="submit" disabled={busy !== null}>Add address</button>
          </div>
        </form>
      ) : null}

      {loading ? <div class="clean-empty"><strong>Loading wallets…</strong></div> : accounts.length ? (
        <div class="wallet-account-list">
          {accounts.map((account) => (
            <AccountRow
              key={account.id}
              account={account}
              busy={busy === account.id}
              onRefresh={() => void refreshOne(account)}
              onRemove={() => void remove(account)}
            />
          ))}
        </div>
      ) : (
        <div class="clean-empty wallet-empty">
          <strong>No external wallets tracked</strong>
          <span>Connect a browser wallet, add a Ledger receive address, or <button type="button" onClick={() => setAddMode("manual")}>enter any public address</button>.</span>
        </div>
      )}

      <p class="wallet-security-note">Omni stores public addresses only. Connecting does not prove ownership, grant spending access, or request a signature.</p>
    </section>
  );
}
