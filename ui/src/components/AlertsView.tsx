import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import {
  describeCondition,
  describeLastFired,
  listAlerts,
  listFirings,
  type Alert,
  type Firing,
} from "../lib/alerts";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";
import { CreateAlertForm } from "./CreateAlertForm";

type State =
  | { kind: "loading" }
  | { kind: "auth" }
  | { kind: "ok"; alerts: Alert[] }
  | { kind: "error"; message: string; detail?: string };

export function AlertsView() {
  const [state, setState] = useState<State>({ kind: "loading" });

  async function reload() {
    setState({ kind: "loading" });
    try {
      const res = await listAlerts();
      setState({ kind: "ok", alerts: res.alerts });
    } catch (err) {
      if (err instanceof AuthRequiredError) {
        setState({ kind: "auth" });
      } else {
        const { message, detail } = describeError(err);
        setState({ kind: "error", message, detail });
      }
    }
  }

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div class="alerts-view">
      <header class="page-head">
        <h1>Alerts</h1>
        <p class="muted">
          An alert watches a condition on a ticker and notifies you the moment
          it&rsquo;s met. Pick one of the condition types below.
        </p>
      </header>

      <section class="panel">
        <h2 class="panel-title">Create an alert</h2>
        <CreateAlertForm onCreated={() => void reload()} />
      </section>

      <section class="panel">
        <h2 class="panel-title">Configured alerts</h2>
        {state.kind === "loading" ? (
          <Loading label={"Loading alerts\u2026"} />
        ) : null}
        {state.kind === "auth" ? (
          <div style={{ padding: "18px" }}>
            <p>
              Alerts are private to their owner. A request without a verified
              token is refused with 401, and rendering an empty list would
              pretend the server answered with your alerts when it did not.
            </p>
            <p style={{ marginTop: "12px" }}>
              <a class="search-btn" href="/login" style={{ textDecoration: "none" }}>
                Sign in
              </a>
            </p>
          </div>
        ) : null}
        {state.kind === "error" ? (
          <ErrorState message={state.message} detail={state.detail} />
        ) : null}
        {state.kind === "ok" && state.alerts.length === 0 ? (
          <p class="empty">
            No alerts set. Create one above to be notified when a condition is met.
          </p>
        ) : null}
        {state.kind === "ok" && state.alerts.length > 0 ? (
          <ul class="gaps">
            {state.alerts.map((a) => (
              <AlertRow key={a.id} alert={a} />
            ))}
          </ul>
        ) : null}
      </section>
    </div>
  );
}

type FiringsState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; firings: Firing[] }
  | { kind: "error"; message: string; detail?: string };

const activeBadge = (active: boolean) =>
  ({
    display: "inline-block",
    padding: "2px 8px",
    borderRadius: "4px",
    border: "1px solid var(--border-strong)",
    fontFamily: "var(--mono)",
    fontSize: "11px",
    color: active ? "var(--tier-fresh)" : "var(--faint)",
  }) as const;

function AlertRow({ alert }: { alert: Alert }) {
  const [open, setOpen] = useState(false);
  const [firings, setFirings] = useState<FiringsState>({ kind: "idle" });

  async function loadFirings() {
    setFirings({ kind: "loading" });
    try {
      const res = await listFirings(alert.id);
      setFirings({ kind: "ok", firings: res.firings });
    } catch (err) {
      if (err instanceof AuthRequiredError) {
        setFirings({
          kind: "error",
          message: "Authentication required",
          detail: "Your token is missing or no longer valid.",
        });
      } else {
        const { message, detail } = describeError(err);
        setFirings({ kind: "error", message, detail });
      }
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && firings.kind === "idle") void loadFirings();
  }

  return (
    <li class="gap-row">
      <div class="gap-head">
        <span class="gap-type">
          {alert.claim_type}
          <span class="gap-key"> &middot; {describeCondition(alert.condition)}</span>
        </span>
        <span style={activeBadge(alert.active)}>
          {alert.active ? "active" : "paused"}
        </span>
      </div>
      <div class="gap-meta">
        <a href={`/entity/${alert.entity_id}`}>entity {alert.entity_id.slice(0, 8)}</a>
        <span class="faint">{describeLastFired(alert.last_fired_at)}</span>
        {alert.created_at ? (
          <span class="faint">created {alert.created_at.slice(0, 10)}</span>
        ) : null}
        <button
          type="button"
          onClick={toggle}
          style={{
            background: "transparent",
            border: "none",
            color: "var(--accent)",
            cursor: "pointer",
            font: "inherit",
            fontSize: "13px",
            padding: 0,
            textDecoration: "underline",
          }}
        >
          {open ? "Hide firings" : "Show firings"}
        </button>
      </div>

      {open ? (
        <div style={{ marginTop: "10px" }}>
          {firings.kind === "idle" ? null : null}
          {firings.kind === "loading" ? (
            <Loading label={"Loading firings\u2026"} />
          ) : null}
          {firings.kind === "error" ? (
            <ErrorState message={firings.message} detail={firings.detail} />
          ) : null}
          {firings.kind === "ok" && firings.firings.length === 0 ? (
            <p class="empty" style={{ padding: "8px 0" }}>
              This alert has not fired. No claim has met the condition yet.
            </p>
          ) : null}
          {firings.kind === "ok" && firings.firings.length > 0 ? (
            <ul class="gaps" style={{ marginTop: 0 }}>
              {firings.firings.map((f) => (
                <li class="gap-row" key={f.claim_id} style={{ paddingLeft: 0, paddingRight: 0 }}>
                  <div class="gap-meta">
                    <span>
                      fired{" "}
                      <strong>
                        {f.fired_at ? f.fired_at.slice(0, 19).replace("T", " ") : "?"}
                      </strong>
                    </span>
                    {f.source ? <span>source {f.source}</span> : null}
                    {f.event_date ? (
                      <span class="faint">event {f.event_date.slice(0, 10)}</span>
                    ) : null}
                  </div>
                  <pre class="gap-detail">{JSON.stringify(f.value, null, 2)}</pre>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
