import { useCallback, useEffect, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError } from "../lib/api";
import { AuthRequiredError, changePassword } from "../lib/auth";
import {
  getSettings,
  getVenueStatus,
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
  | { kind: "ok"; webhookConfigured: boolean; email: string | null; smtpAvailable: boolean }
  | { kind: "error"; message: string };

type State =
  | { kind: "loading" }
  | { kind: "auth" }
  | { kind: "ok"; data: SettingsData; live: VenueStatusResponse }
  | { kind: "error"; message: string; detail?: string };

export function SettingsView() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [pw, setPw] = useState<PwState>({ kind: "idle" });
  const [notify, setNotify] = useState<NotifyState>({ kind: "loading" });
  const [webhookUrl, setWebhookUrl] = useState("");
  const [notifyEmail, setNotifyEmail] = useState("");
  const [notifyMsg, setNotifyMsg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    import("../lib/settings")
      .then(({ getNotifications }) => getNotifications())
      .then((data) => {
        if (!cancelled) {
          setNotify({
            kind: "ok",
            webhookConfigured: data.webhook_configured,
            email: data.email,
            smtpAvailable: data.smtp_available,
          });
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
    if (notify.kind !== "ok") return;
    setNotifyMsg("Saving…");
    try {
      const { putNotifications, testNotifications } = await import("../lib/settings");
      const saved = await putNotifications({
        webhook_url: webhookUrl.trim() || undefined,
        email: notifyEmail.trim() || undefined,
      });
      setNotify({
        kind: "ok",
        webhookConfigured: saved.webhook_configured,
        email: saved.email,
        smtpAvailable: saved.smtp_available,
      });
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
    } catch (err) {
      const described = describeError(err);
      setPw({ kind: "error", message: described.message });
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
  const configurableVenues = state.data.venue_catalog.filter((venue) => venue.connectable);
  const configuredVenues = configurableVenues.filter((venue) => venue.configured).length;
  const categories = providers.reduce<Record<string, ProviderEntry[]>>((grouped, provider) => {
    (grouped[provider.category || "Other"] ||= []).push(provider);
    return grouped;
  }, {});

  return (
    <div class="settings-view product-page">
      <header class="settings-page-heading">
        <div>
          <h1>Settings</h1>
          <p>Connections, data sources, and deployment configuration.</p>
        </div>
        <div class="settings-summary">
          <span><strong>{configuredVenues}/{configurableVenues.length}</strong> user-managed venues configured</span>
          <span><strong>{configuredProviders}</strong> data sources configured</span>
        </div>
      </header>

      <section class="settings-section">
        <div class="section-heading settings-section-heading">
          <div><p class="eyebrow">Trading</p><h2>Venue connections</h2></div>
          <p>Connection state is checked live; desired state is shown separately.</p>
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

      <section class="settings-section">
        <div class="section-heading settings-section-heading">
          <div><p class="eyebrow">Account</p><h2>Password</h2></div>
        </div>
        <form class="auth-form password-form" onSubmit={onPasswordChange}>
          <label>
            <span class="auth-label">Current password</span>
            <input
              class="search-input"
              type="password"
              required
              autocomplete="current-password"
              value={oldPw}
              onInput={(e) => setOldPw((e.target as HTMLInputElement).value)}
            />
          </label>
          <label>
            <span class="auth-label">New password</span>
            <input
              class="search-input"
              type="password"
              required
              minlength={12}
              autocomplete="new-password"
              value={newPw}
              onInput={(e) => setNewPw((e.target as HTMLInputElement).value)}
            />
            <span class="field-help">At least 12 characters.</span>
          </label>
          <button class="search-btn" type="submit" disabled={pw.kind === "busy"}>
            {pw.kind === "busy" ? "Changing…" : "Change password"}
          </button>
          {pw.kind === "done" ? (
            <p class="alert-feedback">Password changed.</p>
          ) : null}
          {pw.kind === "error" ? (
            <p class="auth-error">{pw.message}</p>
          ) : null}
        </form>
      </section>

      <section class="settings-section surface-card">
        <div class="section-heading settings-section-heading">
          <div><p class="eyebrow">Notifications</p><h2>Alert delivery</h2></div>
        </div>
        <p class="settings-lead">
          Where fired alerts are sent. A webhook receives one JSON POST per
          firing batch (ntfy, a bridge script, a chat hook); email needs the
          deployment&rsquo;s SMTP configuration.
          {notify.kind === "ok" && !notify.smtpAvailable
            ? " This deployment has no SMTP configured — email is listed but will not send until the operator adds it."
            : null}
        </p>
        {notify.kind === "loading" ? <Loading label="Loading notifications…" /> : null}
        {notify.kind === "error" ? (
          <p class="auth-error">Notification settings unavailable.</p>
        ) : null}
        {notify.kind === "ok" ? (
          <form class="auth-form password-form" onSubmit={(e) => void saveNotifications(e)}>
            <label>
              <span class="auth-label">Webhook URL</span>
              <input
                class="search-input"
                type="url"
                placeholder={notify.webhookConfigured
                  ? "Configured — paste to replace"
                  : "https://…"}
                value={webhookUrl}
                onInput={(e) => setWebhookUrl((e.target as HTMLInputElement).value)}
              />
              {notify.webhookConfigured && webhookUrl.trim() === "" ? (
                <span class="field-help">A webhook is configured. Saving with this empty removes it.</span>
              ) : null}
            </label>
            <label>
              <span class="auth-label">Email address</span>
              <input
                class="search-input"
                type="email"
                placeholder="you@example.com"
                value={notifyEmail}
                onInput={(e) => setNotifyEmail((e.target as HTMLInputElement).value)}
              />
            </label>
            <button class="search-btn" type="submit">
              Save and send test
            </button>
            {notifyMsg ? <p class="alert-feedback">{notifyMsg}</p> : null}
          </form>
        ) : null}
      </section>

      <section class="settings-section surface-card provider-settings">
        <div class="section-heading settings-section-heading">
          <div><p class="eyebrow">Research</p><h2>Data sources</h2></div>
          <span class="count-badge">{configuredProviders}/{providers.length}</span>
        </div>
        <p class="settings-lead">
          Provider keys are read from deployment secrets. This page reports their state without returning the keys to the browser.
        </p>
        {Object.entries(categories).map(([category, entries]) => (
          <div class="provider-category" key={category}>
            <h3>{category}</h3>
            <div class="provider-status-list">
              {entries.map((provider) => (
                <div class="provider-status-row" key={provider.key}>
                  <span class={`connection-state-dot ${provider.configured ? "is-configured" : ""}`} aria-hidden="true" />
                  <strong>{provider.label}</strong>
                  <span>{provider.configured ? "Configured" : provider.key_required ? "Key needed" : "Available without a key"}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
        {unavailable.length > 0 ? (
          <details class="unavailable-providers">
            <summary>{unavailable.length} catalogued providers are not connected in this build</summary>
            <p>{unavailable.map((provider) => provider.label).join(", ")}</p>
          </details>
        ) : null}
      </section>
    </div>
  );
}
