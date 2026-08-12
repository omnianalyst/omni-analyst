import { useEffect, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError, request } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

interface ProviderEntry {
  key: string;
  label: string;
  category: string;
  settings_field: string;
  key_required: boolean;
  wired: boolean;
  configured: boolean;
}

interface VenueEntry {
  key: string;
  label: string;
  type: string;
  requires_process: boolean;
  description: string;
  configured: boolean;
  enabled: boolean;
  configuration_source: "deployment" | "legacy" | "unavailable";
}

interface SettingsData {
  provider_catalog: ProviderEntry[];
  venue_catalog: VenueEntry[];
}

type State =
  | { kind: "loading" }
  | { kind: "auth" }
  | { kind: "ok"; data: SettingsData }
  | { kind: "error"; message: string; detail?: string };

export function SettingsView() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    const headers = authHeaderIfPresent();
    if (!headers.authorization) {
      setState({ kind: "auth" });
      return;
    }
    request<SettingsData>("/settings/config", headers)
      .then((data) => {
        if (!cancelled) setState({ kind: "ok", data });
      })
      .catch((error) => {
        if (cancelled) return;
        if (error instanceof AuthRequiredError) {
          setState({ kind: "auth" });
          return;
        }
        const described = describeError(error);
        setState({ kind: "error", message: described.message, detail: described.detail });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "loading") return <Loading label="Loading settings…" />;
  if (state.kind === "auth") return <ErrorState message="Sign in to view settings." />;
  if (state.kind === "error") return <ErrorState message={state.message} detail={state.detail} />;

  const providers = state.data.provider_catalog.filter((provider) => provider.wired);
  const unavailable = state.data.provider_catalog.filter((provider) => !provider.wired);
  const configuredProviders = providers.filter((provider) => provider.configured).length;
  const configuredVenues = state.data.venue_catalog.filter((venue) => venue.configured).length;
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
          <span><strong>{configuredVenues}</strong> venues configured</span>
          <span><strong>{configuredProviders}</strong> data sources configured</span>
        </div>
      </header>

      <section class="settings-section">
        <div class="section-heading settings-section-heading">
          <div><p class="eyebrow">Trading</p><h2>Venue connections</h2></div>
          <p>Trading secrets stay outside the browser.</p>
        </div>
        <div class="connection-grid">
          {state.data.venue_catalog.map((venue) => (
            <article class="connection-card" key={venue.key}>
              <div class="connection-header">
                <div>
                  <span class="connection-type">{venue.type}</span>
                  <h3>{venue.label}</h3>
                  <p>{venue.description}</p>
                </div>
                <span class={`connection-state-dot ${venue.configured ? "is-configured" : ""}`} aria-hidden="true" />
              </div>
              <div class="connection-status-row">
                <span class={`connection-status ${venue.configured ? "status-enabled" : ""}`}>
                  {venue.configured ? "Configured" : "Not configured"}
                </span>
                {venue.enabled ? <span>Enabled</span> : null}
              </div>
              <p class="connection-guidance">
                {venue.configuration_source === "deployment"
                  ? venue.configured
                    ? "Credentials were loaded from the deployment environment."
                    : "Add the required credentials to the deployment environment."
                  : venue.configuration_source === "legacy"
                    ? "A legacy saved configuration exists. Move it to secure deployment secrets before changing it."
                    : "Browser-based secret storage is unavailable until an encrypted credential store is connected."}
              </p>
              {venue.requires_process ? <p class="connection-note">Also requires the managed IB Gateway process.</p> : null}
            </article>
          ))}
        </div>
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
