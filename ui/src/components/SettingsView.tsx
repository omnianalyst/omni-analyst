import { useCallback, useEffect, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError } from "../lib/api";
import { AuthRequiredError, changePassword } from "../lib/auth";
import {
  deleteDataKey,
  getDataKeys,
  getNotifications,
  getSettings,
  getVenueStatus,
  putDataKey,
  putNotifications,
  testNotifications,
  type DataKeyProvider,
  type NotificationsState,
  type SettingsData,
  type VenueStatusResponse,
} from "../lib/settings";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";
import { VenueCard } from "./VenueCard";

type PwState =
  | { kind: "idle" }
  | { kind: "busy" }
  | { kind: "done" }
  | { kind: "error"; message: string };

type NotifyState =
  | { kind: "loading" }
  | { kind: "ok"; data: NotificationsState }
  | { kind: "error"; message: string };

type State =
  | { kind: "loading" }
  | { kind: "auth" }
  | { kind: "ok"; data: SettingsData; live: VenueStatusResponse }
  | { kind: "error"; message: string; detail?: string };

// The page's one shape: a quiet card per concern, rows of label + state +
// one action inside it. Nothing expands unless asked; nothing that cannot
// act looks like it can (the same rule VenueCard holds).

function Row({
  label,
  hint,
  state,
  children,
}: {
  label: string;
  hint?: string;
  state?: string;
  children?: preact.ComponentChildren;
}) {
  return (
    <div class="settings-row">
      <div class="settings-row-label">
        <strong>{label}</strong>
        {hint ? <small>{hint}</small> : null}
      </div>
      {state ? <span class="settings-row-state">{state}</span> : null}
      {children ? <div class="settings-row-actions">{children}</div> : null}
    </div>
  );
}

export function SettingsView() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [pwOpen, setPwOpen] = useState(false);
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [pw, setPw] = useState<PwState>({ kind: "idle" });
  const [notifyOpen, setNotifyOpen] = useState(false);
  const [notify, setNotify] = useState<NotifyState>({ kind: "loading" });
  const [webhookUrl, setWebhookUrl] = useState("");
  const [notifyEmail, setNotifyEmail] = useState("");
  const [notifyMsg, setNotifyMsg] = useState<string | null>(null);
  const [dataKeys, setDataKeys] = useState<DataKeyProvider[] | null>(null);
  const [backupBusy, setBackupBusy] = useState(false);
  const [backupNote, setBackupNote] = useState<string | null>(null);

  async function downloadBackup() {
    setBackupBusy(true);
    setBackupNote("Preparing backup… this streams the whole store and can take a minute.");
    try {
      const { getAuthToken } = await import("../lib/auth");
      const token = getAuthToken();
      const res = await fetch("/settings/backup", {
        headers: token ? { authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        setBackupNote(`Backup failed: ${text.slice(0, 120) || res.status}`);
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `omni-backup-${new Date().toISOString().slice(0, 10)}.dump`;
      a.click();
      URL.revokeObjectURL(url);
      setBackupNote(
        "Downloaded. Restore is a documented two-command step (DEPLOY.md, Moving machines) -- deliberately not a button.",
      );
    } catch (err) {
      setBackupNote(`Backup failed: ${describeError(err).message}`);
    } finally {
      setBackupBusy(false);
    }
  }
  const [keyDraft, setKeyDraft] = useState<Record<string, string>>({});
  const [keyBusy, setKeyBusy] = useState<string | null>(null);
  const [keyMsg, setKeyMsg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getDataKeys()
      .then((res) => { if (!cancelled) setDataKeys(res.providers); })
      .catch(() => { if (!cancelled) setDataKeys([]); });
    void getNotifications()
      .then((data) => {
        if (!cancelled) {
          setNotify({ kind: "ok", data });
          setNotifyEmail(data.email ?? "");
        }
      })
      .catch(() => {
        if (!cancelled) setNotify({ kind: "error", message: "unavailable" });
      });
    return () => { cancelled = true; };
  }, []);

  async function saveNotifications(e: Event) {
    e.preventDefault();
    setNotifyMsg("Saving…");
    try {
      const saved = await putNotifications({
        webhook_url: webhookUrl.trim() || undefined,
        email: notifyEmail.trim() || undefined,
      });
      setNotify({ kind: "ok", data: saved });
      setWebhookUrl("");
      try {
        const test = await testNotifications();
        setNotifyMsg(`Saved. Test sent through: ${test.sent.join(", ")}.`);
      } catch {
        setNotifyMsg("Saved. (Test delivery unavailable.)");
      }
    } catch (err) {
      setNotifyMsg(describeError(err).message);
    }
  }

  async function saveKey(provider: string) {
    const value = (keyDraft[provider] ?? "").trim();
    setKeyBusy(provider);
    setKeyMsg(null);
    try {
      await putDataKey(provider, value);
      setKeyDraft((d) => ({ ...d, [provider]: "" }));
      const res = await getDataKeys();
      setDataKeys(res.providers);
      setKeyMsg(
        value === ""
          ? "Key removed. The deployment's own key (if set) applies again."
          : "Key stored, encrypted. New fetches use it.",
      );
    } catch (err) {
      setKeyMsg(describeError(err).message);
    } finally {
      setKeyBusy(null);
    }
  }

  async function removeKey(provider: string) {
    setKeyBusy(provider);
    setKeyMsg(null);
    try {
      await deleteDataKey(provider);
      const res = await getDataKeys();
      setDataKeys(res.providers);
      setKeyMsg("Key removed.");
    } catch (err) {
      setKeyMsg(describeError(err).message);
    } finally {
      setKeyBusy(null);
    }
  }

  async function onPasswordChange(e: Event) {
    e.preventDefault();
    if (pw.kind === "busy") return;
    setPw({ kind: "busy" });
    try {
      await changePassword(oldPw, newPw);
      setOldPw("");
      setNewPw("");
      setPw({ kind: "done" });
      setPwOpen(false);
    } catch (err) {
      setPw({ kind: "error", message: describeError(err).message });
    }
  }

  const load = useCallback(() => {
    if (!authHeaderIfPresent().authorization) {
      setState({ kind: "auth" });
      return;
    }
    Promise.all([getSettings(), getVenueStatus()])
      .then(([data, live]) => setState({ kind: "ok", data, live }))
      .catch((error) => {
        if (error instanceof AuthRequiredError) {
          setState({ kind: "auth" });
          return;
        }
        const described = describeError(error);
        setState({ kind: "error", message: described.message, detail: described.detail });
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (state.kind === "loading") return <Loading label="Loading settings…" />;
  if (state.kind === "auth") return <ErrorState message="Sign in to view settings." />;
  if (state.kind === "error") return <ErrorState message={state.message} detail={state.detail} />;


  const notifyChannels = notify.kind === "ok"
    ? [
        notify.data.webhook_configured ? "Webhook" : null,
        notify.data.email ? "Email" : null,
      ].filter((x): x is string => x !== null)
    : [];

  return (
    <div class="settings-view product-page">
      <header class="settings-page-heading">
        <div>
          <h1>Settings</h1>
          <p>Account, notifications, connections, and data sources.</p>
        </div>
        <div class="settings-summary">
          <span>Keys run your fetches · encrypted at rest</span>
        </div>
      </header>

      <section class="surface-card settings-card">
        <div class="section-heading">
          <div><p class="eyebrow">Account</p><h2>Password</h2></div>
        </div>
        <Row
          label="Password"
          hint="Rotate your own sign-in."
          state="Set"
        >
          <button
            type="button"
            class="btn-secondary compact-button"
            onClick={() => { setPwOpen((v) => !v); setPw({ kind: "idle" }); }}
          >
            {pwOpen ? "Cancel" : "Change"}
          </button>
        </Row>
        {pwOpen ? (
          <form class="settings-inline-form" onSubmit={onPasswordChange}>
            <label>
              <span>Current password</span>
              <input
                type="password"
                required
                autocomplete="current-password"
                value={oldPw}
                onInput={(e) => setOldPw((e.target as HTMLInputElement).value)}
              />
            </label>
            <label>
              <span>New password</span>
              <input
                type="password"
                required
                minlength={12}
                autocomplete="new-password"
                value={newPw}
                onInput={(e) => setNewPw((e.target as HTMLInputElement).value)}
              />
              <small>At least 12 characters.</small>
            </label>
            <button class="btn-primary" type="submit" disabled={pw.kind === "busy"}>
              {pw.kind === "busy" ? "Changing…" : "Save password"}
            </button>
            {pw.kind === "error" ? <p class="inline-warning">{pw.message}</p> : null}
          </form>
        ) : null}
        {pw.kind === "done" ? <p class="settings-row-note">Password changed.</p> : null}
      </section>

      <section class="surface-card settings-card">
        <div class="section-heading">
          <div><p class="eyebrow">Notifications</p><h2>Alert delivery</h2></div>
        </div>
        {notify.kind === "loading" ? (
          <p class="settings-row-note">Reading channels…</p>
        ) : null}
        {notify.kind === "error" ? (
          <p class="settings-row-note">Notification settings unavailable.</p>
        ) : null}
        {notify.kind === "ok" ? (
          <>
            <Row
              label="Channels"
              hint="Where fired alerts are sent."
              state={notifyChannels.length > 0 ? notifyChannels.join(" · ") : "None"}
            >
              <button
                type="button"
                class="btn-secondary compact-button"
                onClick={() => { setNotifyOpen((v) => !v); setNotifyMsg(null); }}
              >
                {notifyOpen ? "Close" : "Edit"}
              </button>
            </Row>
            {notify.kind === "ok" && !notify.data.smtp_available ? (
              <p class="settings-row-note">
                Email needs the deployment&rsquo;s SMTP configuration — a webhook works today.
              </p>
            ) : null}
            {notifyOpen ? (
              <form class="settings-inline-form" onSubmit={(e) => void saveNotifications(e)}>
                <label>
                  <span>Webhook URL</span>
                  <input
                    type="url"
                    placeholder={
                      notify.kind === "ok" && notify.data.webhook_configured
                        ? "Configured — paste to replace"
                        : "https://…"
                    }
                    value={webhookUrl}
                    onInput={(e) => setWebhookUrl((e.target as HTMLInputElement).value)}
                  />
                  {notify.kind === "ok" && notify.data.webhook_configured && webhookUrl.trim() === "" ? (
                    <small>Empty on save removes the configured webhook.</small>
                  ) : null}
                </label>
                <label>
                  <span>Email</span>
                  <input
                    type="email"
                    placeholder="you@example.com"
                    value={notifyEmail}
                    onInput={(e) => setNotifyEmail((e.target as HTMLInputElement).value)}
                  />
                </label>
                <button class="btn-primary" type="submit">Save and send test</button>
                {notifyMsg ? <p class="settings-row-note">{notifyMsg}</p> : null}
              </form>
            ) : null}
          </>
        ) : null}
      </section>

      <section class="surface-card settings-card">
        <div class="section-heading">
          <div><p class="eyebrow">Trading</p><h2>Venue connections</h2></div>
        </div>
        <div class="connection-grid">
          {state.data.venue_catalog.map((venue) => (
            <VenueCard
              key={venue.key}
              entry={venue}
              status={state.live.venues.find((item) => item.key === venue.key)}
              onChanged={load}
            />
          ))}
        </div>
      </section>

      <section class="surface-card settings-card">
        <div class="section-heading">
          <div><p class="eyebrow">Instance</p><h2>Backup</h2></div>
        </div>
        <Row
          label="Full backup"
          hint="Everything: claims, predictions, calibration, watchlists. Custom-format dump; restore is documented, not a button."
        >
          <button
            type="button"
            class="btn-secondary compact-button"
            disabled={backupBusy}
            onClick={() => void downloadBackup()}
          >
            {backupBusy ? "Preparing…" : "Download"}
          </button>
        </Row>
        {backupNote ? <p class="settings-row-note">{backupNote}</p> : null}
      </section>

      <section class="surface-card settings-card">
        <div class="section-heading">
          <div><p class="eyebrow">Data sources</p><h2>Keys</h2></div>
        </div>
        <p class="settings-row-note">
          The system runs with zero keys (crypto, macro, fundamentals, news).
          Equity and ETF prices need a Polygon key — the free tier is enough.
          Pasted keys are encrypted at rest and used for your fetches.
        </p>
        {dataKeys === null ? (
          <p class="settings-row-note">Reading keys…</p>
        ) : (
          dataKeys.map((p) => (
            <div class="settings-row" key={p.key}>
              <div class="settings-row-label">
                <strong>{p.key === "fred" ? "FRED" : p.key.charAt(0).toUpperCase() + p.key.slice(1)}</strong>
                <small>{p.description}</small>
              </div>
              <span class="settings-row-state">{p.configured ? "Key set" : "—"}</span>
              <div class="settings-row-actions">
                <input
                  class="key-input"
                  type="password"
                  placeholder={p.configured ? "Replace" : "Paste key"}
                  value={keyDraft[p.key] ?? ""}
                  onInput={(e) =>
                    setKeyDraft((d) => ({ ...d, [p.key]: (e.target as HTMLInputElement).value }))}
                  aria-label={`${p.key} key`}
                />
                <button
                  type="button"
                  class="btn-secondary compact-button"
                  disabled={keyBusy === p.key || (keyDraft[p.key] ?? "").trim() === ""}
                  onClick={() => void saveKey(p.key)}
                >
                  {keyBusy === p.key ? "Saving…" : "Save"}
                </button>
                {p.configured ? (
                  <button
                    type="button"
                    class="btn-secondary compact-button"
                    disabled={keyBusy === p.key}
                    onClick={() => void removeKey(p.key)}
                  >
                    Remove
                  </button>
                ) : null}
              </div>
            </div>
          ))
        )}
        {keyMsg ? <p class="settings-row-note">{keyMsg}</p> : null}
        <p class="settings-row-note">
          Included without keys: SEC EDGAR, World Bank, DefiLlama, RSS news,
          and the crypto venues (Binance, Coinbase, Kraken, Bybit, OKX).
        </p>
      </section>
    </div>
  );
}
