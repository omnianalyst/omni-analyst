import { useCallback, useEffect, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError } from "../lib/api";
import { AuthRequiredError, changePassword } from "../lib/auth";
import {
  getNotifications,
  getSettings,
  getVenueStatus,
  putNotifications,
  testNotifications,
  type NotificationsState,
  type ProviderEntry,
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

  useEffect(() => {
    let cancelled = false;
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

  const providers = state.data.provider_catalog.filter((provider) => provider.wired);
  const unavailable = state.data.provider_catalog.filter((provider) => !provider.wired);
  const configuredProviders = providers.filter((provider) => provider.configured).length;
  const categories = providers.reduce<Record<string, ProviderEntry[]>>((grouped, provider) => {
    (grouped[provider.category || "Other"] ||= []).push(provider);
    return grouped;
  }, {});

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
          <span><strong>{configuredProviders}</strong> of {providers.length} data sources active</span>
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
          <div><p class="eyebrow">Data sources</p><h2>Provider keys</h2></div>
          <span class="count-badge">{configuredProviders}/{providers.length}</span>
        </div>
        <p class="settings-row-note">
          Keys live in the deployment&rsquo;s environment, not the browser. This
          is each source&rsquo;s state.
        </p>
        {Object.entries(categories).map(([category, entries]) => (
          <div class="provider-category" key={category}>
            <h3>{category}</h3>
            <div class="provider-status-list">
              {entries.map((provider) => (
                <div class="provider-status-row" key={provider.key}>
                  <span class={`connection-state-dot ${provider.configured ? "is-configured" : ""}`} aria-hidden="true" />
                  <strong>{provider.label}</strong>
                  <span>{provider.configured ? "Active" : provider.key_required ? "Key needed" : "No key required"}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
        {unavailable.length > 0 ? (
          <details class="unavailable-providers">
            <summary>{unavailable.length} catalogued but not connected in this build</summary>
            <p>{unavailable.map((provider) => provider.label).join(", ")}</p>
          </details>
        ) : null}
      </section>
    </div>
  );
}
