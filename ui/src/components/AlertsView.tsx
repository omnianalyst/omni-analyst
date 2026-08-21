import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import {
  ackAll,
  ackFiring,
  buildCondition,
  CONDITION_KINDS,
  conditionForm,
  conditionLabel,
  deleteAlert,
  describeCondition,
  describeLastFired,
  listAlerts,
  listFirings,
  listInbox,
  updateAlert,
  type Alert,
  type ConditionFormState,
  type ConditionKind,
  type Firing,
  type InboxFiring,
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
  const [notice, setNotice] = useState<string | null>(null);
  const [unread, setUnread] = useState(0);

  function replaceAlert(next: Alert) {
    setState((current) => current.kind === "ok"
      ? { ...current, alerts: current.alerts.map((alert) => alert.id === next.id ? next : alert) }
      : current);
  }

  function dropAlert(id: string) {
    setState((current) => current.kind === "ok"
      ? { ...current, alerts: current.alerts.filter((alert) => alert.id !== id) }
      : current);
    setNotice("Alert deleted.");
  }

  async function reload() {
    setState({ kind: "loading" });
    try {
      const res = await listAlerts();
      setState({ kind: "ok", alerts: res.alerts });
      try {
        setUnread((await listInbox()).unread);
      } catch {
        setUnread(0);
      }
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
        <h1>
          Alerts
          {unread > 0 ? <span class="count-badge count-warning unread-badge">{unread} new</span> : null}
        </h1>
        <p class="muted">
          An alert watches a condition on a ticker and fires the moment it&rsquo;s
          crossed. Deliver to a webhook or email from{" "}
          <a href="/settings">Settings</a>.
        </p>
      </header>

      <section class="panel">
        <h2 class="panel-title">Create an alert</h2>
        <CreateAlertForm onCreated={() => void reload()} />
      </section>

      <section class="panel">
        <h2 class="panel-title">Configured alerts</h2>
        {notice ? <p class="alert-feedback alert-panel-feedback" role="status">{notice}</p> : null}
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
              <AlertRow
                key={a.id}
                alert={a}
                onChanged={replaceAlert}
                onDeleted={dropAlert}
              />
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

type ActionState =
  | { kind: "idle" }
  | { kind: "busy" }
  | { kind: "ok"; message: string }
  | { kind: "error"; message: string; detail?: string };

function AlertRow({
  alert,
  onChanged,
  onDeleted,
}: {
  alert: Alert;
  onChanged(alert: Alert): void;
  onDeleted(id: string): void;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<ConditionFormState>(() => conditionForm(alert.condition));
  const [action, setAction] = useState<ActionState>({ kind: "idle" });
  const [firings, setFirings] = useState<FiringsState>({ kind: "idle" });

  function actionError(error: unknown) {
    if (error instanceof AuthRequiredError) {
      setAction({
        kind: "error",
        message: "Authentication required",
        detail: "Your token is missing or no longer valid.",
      });
      return;
    }
    const { message, detail } = describeError(error);
    setAction({ kind: "error", message, detail });
  }

  async function toggleActive() {
    setAction({ kind: "busy" });
    try {
      const next = await updateAlert(alert.id, { active: !alert.active });
      onChanged(next);
      setAction({ kind: "ok", message: next.active ? "Alert resumed." : "Alert paused." });
    } catch (error) {
      actionError(error);
    }
  }

  async function saveCondition(event: Event) {
    event.preventDefault();
    const built = buildCondition(form);
    if (!built.ok) {
      setAction({ kind: "error", message: built.error });
      return;
    }
    setAction({ kind: "busy" });
    try {
      const next = await updateAlert(alert.id, { condition: built.condition });
      onChanged(next);
      setForm(conditionForm(next.condition));
      setEditing(false);
      setAction({ kind: "ok", message: "Alert condition updated." });
    } catch (error) {
      actionError(error);
    }
  }

  async function remove() {
    if (!window.confirm("Delete this alert? Its firing history will no longer be available here.")) return;
    setAction({ kind: "busy" });
    try {
      await deleteAlert(alert.id);
      onDeleted(alert.id);
    } catch (error) {
      actionError(error);
    }
  }

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

  async function ackOne(claimId: string) {
    try {
      await ackFiring(alert.id, claimId);
      await loadFirings();
    } catch (error) {
      actionError(error);
    }
  }

  async function markAllRead() {
    setAction({ kind: "busy" });
    try {
      await ackAll(alert.id);
      await loadFirings();
      setAction({ kind: "ok", message: "All firings marked read." });
    } catch (error) {
      actionError(error);
    }
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
        {alert.one_shot ? (
          <span style={activeBadge(false)}>one-shot</span>
        ) : null}
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
        <button type="button" class="alert-action" disabled={action.kind === "busy"} onClick={() => void markAllRead()}>
          Mark all read
        </button>
        <button type="button" class="alert-action" disabled={action.kind === "busy"} onClick={() => void toggleActive()}>
          {alert.active ? "Pause" : "Resume"}
        </button>
        <button
          type="button"
          class="alert-action"
          disabled={action.kind === "busy"}
          onClick={() => {
            setForm(conditionForm(alert.condition));
            setEditing((value) => !value);
            setAction({ kind: "idle" });
          }}
        >
          {editing ? "Cancel edit" : "Edit"}
        </button>
        <button type="button" class="alert-action alert-action-delete" disabled={action.kind === "busy"} onClick={() => void remove()}>
          Delete
        </button>
      </div>

      {editing ? (
        <form class="alert-edit-form" onSubmit={(event) => void saveCondition(event)}>
          <label>
            Condition
            <select
              value={form.kind}
              onChange={(event) => setForm((current) => ({ ...current, kind: event.currentTarget.value as ConditionKind }))}
            >
              {CONDITION_KINDS.map((kind) => <option key={kind} value={kind}>{conditionLabel(kind)}</option>)}
            </select>
          </label>
          {form.kind === "value_above" || form.kind === "value_below" ? (
            <>
              <label>
                Level
                <input type="number" step="any" value={form.threshold} onInput={(event) => setForm((current) => ({ ...current, threshold: event.currentTarget.value }))} />
              </label>
              <label>
                Field
                <input value={form.field} onInput={(event) => setForm((current) => ({ ...current, field: event.currentTarget.value }))} />
              </label>
            </>
          ) : null}
          {form.kind === "pct_change_above" || form.kind === "pct_change_below" ? (
            <>
              <label>
                Move %
                <input type="number" min="0" step="any" value={form.pct} onInput={(event) => setForm((current) => ({ ...current, pct: event.currentTarget.value }))} />
              </label>
              <label>
                Window (days)
                <input type="number" min="1" step="1" value={form.windowDays} onInput={(event) => setForm((current) => ({ ...current, windowDays: event.currentTarget.value }))} />
              </label>
            </>
          ) : null}
          {form.kind === "staleness_exceeds" ? (
            <label>
              Seconds
              <input type="number" min="0" step="any" value={form.seconds} onInput={(event) => setForm((current) => ({ ...current, seconds: event.currentTarget.value }))} />
            </label>
          ) : null}
          <button type="submit" class="alert-action" disabled={action.kind === "busy"}>Save condition</button>
        </form>
      ) : null}

      {action.kind === "ok" ? <p class="alert-feedback" role="status">{action.message}</p> : null}
      {action.kind === "error" ? <ErrorState message={action.message} detail={action.detail} /> : null}

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
                    {"acknowledged_at" in f && !(f as InboxFiring).acknowledged_at ? (
                      <button
                        type="button"
                        class="alert-action"
                        onClick={() => void ackOne(f.claim_id)}
                      >
                        Mark read
                      </button>
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
