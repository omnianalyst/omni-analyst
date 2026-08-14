import { useCallback, useEffect, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
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

type State =
  | { kind: "loading" }
  | { kind: "auth" }
  | { kind: "ok"; data: SettingsData; live: VenueStatusResponse }
  | { kind: "error"; message: string; detail?: string };

export function SettingsView() {
  const [state, setState] = useState<State>({ kind: "loading" });

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
