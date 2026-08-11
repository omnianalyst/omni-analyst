import { useEffect, useState } from "preact/hooks";
import { request, authHeaderIfPresent, describeError, sendJson } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import { Loading } from "./Loading";
import { ErrorState } from "./ErrorState";

interface ProviderEntry {
  key: string; label: string; category: string; settings_field: string;
  key_required: boolean; wired: boolean;
}
interface VenueEntry {
  key: string; label: string; type: string; requires_process: boolean;
  description: string; fields: { name: string; label: string; type: string; required: boolean }[];
}
interface SettingsData {
  providers: Record<string, string>;
  venues: Record<string, Record<string, unknown>>;
  provider_catalog: ProviderEntry[];
  venue_catalog: VenueEntry[];
}
type State =
  | { kind: "loading" }
  | { kind: "auth" }
  | { kind: "ok"; data: SettingsData; saved: boolean }
  | { kind: "error"; message: string };

export function SettingsView() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [providerKeys, setProviderKeys] = useState<Record<string, string>>({});
  const [venueEnabled, setVenueEnabled] = useState<Record<string, boolean>>({});

  useEffect(() => {
    let cancelled = false;
    const headers = authHeaderIfPresent();
    if (!headers.authorization) { setState({ kind: "auth" }); return; }
    (async () => {
      try {
        const data = await request<SettingsData>("/settings", headers);
        if (!cancelled) {
          setState({ kind: "ok", data, saved: false });
          setProviderKeys(data.providers || {});
          const en: Record<string, boolean> = {};
          for (const v of data.venue_catalog || []) en[v.key] = Boolean(data.venues?.[v.key]?.enabled);
          setVenueEnabled(en);
        }
      } catch (err) {
        if (cancelled) return;
        if (err instanceof AuthRequiredError) { setState({ kind: "auth" }); return; }
        setState({ kind: "error", message: describeError(err).message });
      }
    })();
    return () => { cancelled = true; };
  }, []);

  if (state.kind === "loading") return <Loading label="Loading settings..." />;
  if (state.kind === "auth") return <ErrorState message="Sign in to manage settings." />;
  if (state.kind === "error") return <ErrorState message={state.message} />;

  const { data } = state;
  const headers = authHeaderIfPresent();

  const saveProviders = async () => {
    try {
      await sendJson("POST", "/settings", { providers: providerKeys }, headers);
      setState({ kind: "ok", data, saved: true });
      setTimeout(() => setState((s) => s.kind === "ok" ? { ...s, saved: false } : s), 2000);
    } catch (err) { setState({ kind: "error", message: describeError(err).message }); }
  };

  const toggleVenue = async (key: string, enabled: boolean) => {
    setVenueEnabled((p) => ({ ...p, [key]: enabled }));
    try { await sendJson("POST", `/settings/venue/${key}/toggle`, { enabled }, headers); }
    catch { setVenueEnabled((p) => ({ ...p, [key]: !enabled })); }
  };

  const cats: Record<string, ProviderEntry[]> = {};
  for (const p of data.provider_catalog || []) (cats[p.category || "Other"] ||= []).push(p);

  return (
    <div class="settings-view">
      <header class="page-head">
        <h1>Settings</h1>
        <p class="muted">Manage API keys, data providers, and trading venue connections.</p>
      </header>

      <section class="panel">
        <h2 class="panel-title">Trading Venues</h2>
        <p class="muted" style={{ fontSize: "12px", marginBottom: "16px" }}>
          Enable a venue to connect. IBKR starts a Gateway container automatically.
        </p>
        {(data.venue_catalog || []).map((v) => (
          <div key={v.key} class="venue-row">
            <div class="venue-info">
              <div class="venue-label">{v.label}</div>
              <div class="muted" style={{ fontSize: "12px" }}>{v.description}</div>
              {v.requires_process && (
                <div style={{ fontSize: "11px", color: "var(--yellow, #fbbf24)", marginTop: "4px" }}>
                  Requires IB Gateway container (managed automatically)
                </div>
              )}
            </div>
            <button class={`toggle-switch ${venueEnabled[v.key] ? "toggle-on" : ""}`}
              onClick={() => toggleVenue(v.key, !venueEnabled[v.key])}>
              <span class="toggle-knob" />
            </button>
          </div>
        ))}
      </section>

      <section class="panel">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
          <h2 class="panel-title">Data Provider Keys</h2>
          <button class="btn-primary" onClick={saveProviders}>
            {state.kind === "ok" && state.saved ? "Saved" : "Save Keys"}
          </button>
        </div>
        {Object.entries(cats).map(([cat, providers]) => (
          <div key={cat} class="settings-category">
            <div class="settings-category-label">{cat}</div>
            {providers.map((p) => (
              <div key={p.key} class="settings-field-row">
                <label class="settings-field-label">
                  {p.label}
                  {p.wired ? <span class="settings-wired">wired</span> : <span class="settings-unwired">not wired</span>}
                </label>
                <input type={p.key_required ? "password" : "text"} class="settings-input"
                  placeholder={p.key_required ? "Enter API key..." : "Optional"}
                  value={providerKeys[p.settings_field] || ""}
                  onInput={(e) => setProviderKeys((prev) => ({ ...prev, [p.settings_field]: (e.target as HTMLInputElement).value }))}
                />
              </div>
            ))}
          </div>
        ))}
      </section>
    </div>
  );
}
