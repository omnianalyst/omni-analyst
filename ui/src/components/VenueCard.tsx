import { useState } from "preact/hooks";
import {
  blockedReason,
  clearVenueCredentials,
  describeSource,
  saveVenueCredentials,
  toggleVenue,
  type VenueEntry,
} from "../lib/settings";

type Busy = null | "toggling" | "saving" | "clearing";

/** One venue: what is stored, whether it is on, and how to change both.
 *
 * Two rules this component exists to hold:
 *
 * - A control that cannot act is not shown as if it could. An unconfigured
 *   venue's toggle is disabled with the reason beside it, rather than accepting
 *   a click that silently does nothing.
 * - Enabling is not trading. The label says "connect", because that is all it
 *   does — the trading tier has its own gates and nothing here widens them.
 */
export function VenueCard(
  { entry, onChanged }: { entry: VenueEntry; onChanged: () => void },
) {
  const [busy, setBusy] = useState<Busy>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [message, setMessage] = useState<{ tone: string; text: string } | null>(null);

  const source = describeSource(entry);
  const blocked = blockedReason(entry);
  const editable = entry.configuration_source !== "deployment" && (entry.fields ?? []).length > 0;

  async function run(action: Busy, fn: () => Promise<unknown>, ok: string) {
    setBusy(action);
    setMessage(null);
    try {
      await fn();
      setMessage({ tone: "ok", text: ok });
      onChanged();
    } catch (err) {
      setMessage({
        tone: "warn",
        text: err instanceof Error ? err.message : "That did not work.",
      });
    } finally {
      setBusy(null);
    }
  }

  return (
    <article class="connection-card">
      <div class="connection-header">
        <div>
          <span class="connection-type">{entry.type}</span>
          <h3>{entry.label}</h3>
          <p>{entry.description}</p>
        </div>
        <span
          class={`connection-state-dot ${entry.configured ? "is-configured" : ""}`}
          aria-hidden="true"
        />
      </div>

      <div class="connection-status-row">
        <span class={`connection-status status-${source.tone}`}>{source.label}</span>
        {entry.enabled ? <span class="status-enabled">Connected</span> : null}
      </div>
      <p class="connection-guidance">{source.detail}</p>

      {entry.requires_process ? (
        <p class="connection-note">Also requires the managed IB Gateway process.</p>
      ) : null}

      <div class="venue-actions">
        <label class="venue-toggle">
          <input
            type="checkbox"
            checked={entry.enabled}
            disabled={blocked !== null || busy !== null}
            onChange={(event) =>
              void run(
                "toggling",
                () => toggleVenue(entry.key, (event.target as HTMLInputElement).checked),
                (event.target as HTMLInputElement).checked ? "Connecting…" : "Disconnected.",
              )}
          />
          <span>{entry.enabled ? "Connected" : "Connect"}</span>
        </label>
        {blocked ? <small class="venue-blocked">{blocked}</small> : null}

        {editable ? (
          <button
            type="button"
            class="btn-secondary compact-button"
            onClick={() => setFormOpen((open) => !open)}
            disabled={busy !== null}
          >
            {entry.configured ? "Replace credentials" : "Add credentials"}
          </button>
        ) : null}

        {editable && entry.configured ? (
          <button
            type="button"
            class="btn-secondary compact-button"
            disabled={busy !== null}
            onClick={() =>
              void run("clearing", () => clearVenueCredentials(entry.key), "Credentials cleared.")}
          >
            Clear
          </button>
        ) : null}
      </div>

      {formOpen && editable ? (
        <form
          class="venue-form"
          onSubmit={(event) => {
            event.preventDefault();
            void run(
              "saving",
              () => saveVenueCredentials(entry.key, values),
              "Stored, encrypted.",
            ).then(() => {
              setValues({});
              setFormOpen(false);
            });
          }}
        >
          {(entry.fields ?? []).map((field) => (
            <label key={field.name} class="venue-field">
              <span>
                {field.label}
                {field.required ? <em aria-hidden="true"> *</em> : null}
              </span>
              {field.type === "checkbox" ? (
                <input
                  type="checkbox"
                  checked={Boolean(values[field.name])}
                  onChange={(e) =>
                    setValues((v) => ({
                      ...v,
                      [field.name]: (e.target as HTMLInputElement).checked,
                    }))}
                />
              ) : (
                <input
                  type={field.type}
                  // Never prefilled. The API does not return stored secrets, so
                  // a value here could only come from somewhere it should not.
                  value={(values[field.name] as string) ?? ""}
                  autoComplete="off"
                  spellcheck={false}
                  onInput={(e) =>
                    setValues((v) => ({
                      ...v,
                      [field.name]: (e.target as HTMLInputElement).value,
                    }))}
                />
              )}
            </label>
          ))}
          <div class="venue-form-actions">
            <button type="submit" class="btn-primary compact-button" disabled={busy !== null}>
              {busy === "saving" ? "Storing…" : "Store encrypted"}
            </button>
            <button
              type="button"
              class="btn-secondary compact-button"
              onClick={() => { setFormOpen(false); setValues({}); }}
            >
              Cancel
            </button>
          </div>
          <p class="venue-form-note">
            Stored encrypted under this deployment's credential key and never returned to
            the browser. Connecting a venue does not enable trading.
          </p>
        </form>
      ) : null}

      {message ? (
        <p class={message.tone === "ok" ? "venue-message" : "inline-warning"}>{message.text}</p>
      ) : null}
    </article>
  );
}
