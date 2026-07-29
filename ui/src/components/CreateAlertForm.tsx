import { useState } from "preact/hooks";
import { describeError, searchEntities, type Entity } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import {
  buildCondition,
  CONDITION_KINDS,
  createAlert,
  defaultConditionForm,
  type ConditionFormState,
  type ConditionKind,
} from "../lib/alerts";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

type SearchState =
  | { kind: "idle" }
  | { kind: "loading"; q: string }
  | { kind: "ok"; q: string; entities: Entity[] }
  | { kind: "empty"; q: string }
  | { kind: "error"; message: string; detail?: string };

type CreateState =
  | { kind: "idle" }
  | { kind: "creating" }
  | { kind: "error"; message: string; detail?: string };

const fieldStyle = { display: "grid", gap: "4px" } as const;
const linkBtnStyle = {
  background: "transparent",
  border: "1px solid var(--border-strong)",
  color: "var(--accent)",
  padding: "6px 12px",
  borderRadius: "6px",
  cursor: "pointer",
  font: "inherit",
  fontSize: "13px",
} as const;

function needsThreshold(kind: ConditionKind): boolean {
  return kind === "value_above" || kind === "value_below";
}

export function CreateAlertForm({ onCreated }: { onCreated: () => void }) {
  const [entityQuery, setEntityQuery] = useState("");
  const [entityId, setEntityId] = useState<string | null>(null);
  const [entityLabel, setEntityLabel] = useState<string>("");
  const [search, setSearch] = useState<SearchState>({ kind: "idle" });
  const [claimType, setClaimType] = useState("");
  const [form, setForm] = useState<ConditionFormState>(defaultConditionForm());
  const [create, setCreate] = useState<CreateState>({ kind: "idle" });

  function patchForm(patch: Partial<ConditionFormState>) {
    setForm((prev) => ({ ...prev, ...patch }));
  }

  async function onSearch(e: Event) {
    e.preventDefault();
    const q = entityQuery.trim();
    if (!q) {
      setSearch({ kind: "idle" });
      return;
    }
    setSearch({ kind: "loading", q });
    try {
      const res = await searchEntities(q);
      if (res.entities.length === 0) {
        setSearch({ kind: "empty", q });
      } else {
        setSearch({ kind: "ok", q, entities: res.entities });
      }
    } catch (err) {
      const { message, detail } = describeError(err);
      setSearch({ kind: "error", message, detail });
    }
  }

  function pickEntity(ent: Entity) {
    setEntityId(ent.id);
    setEntityLabel(
      `${ent.symbol ?? "\u2014"} \u00b7 ${ent.name ?? "(unnamed)"}`,
    );
    setSearch({ kind: "idle" });
    setEntityQuery("");
  }

  async function onSubmit(e: Event) {
    e.preventDefault();
    if (create.kind === "creating") return;
    if (!entityId) {
      setCreate({
        kind: "error",
        message: "Choose an entity to attach the alert to.",
      });
      return;
    }
    const ct = claimType.trim();
    if (!ct) {
      setCreate({
        kind: "error",
        message: "claim_type is required.",
      });
      return;
    }
    const built = buildCondition(form);
    if (!built.ok) {
      setCreate({ kind: "error", message: built.error });
      return;
    }
    setCreate({ kind: "creating" });
    try {
      await createAlert({
        entity_id: entityId,
        claim_type: ct,
        condition: built.condition,
      });
      setCreate({ kind: "idle" });
      setEntityId(null);
      setEntityLabel("");
      setClaimType("");
      setForm(defaultConditionForm());
      onCreated();
    } catch (err) {
      if (err instanceof AuthRequiredError) {
        setCreate({
          kind: "error",
          message: "Authentication required",
          detail: "Your token is missing or no longer valid.",
        });
      } else {
        const { message, detail } = describeError(err);
        setCreate({ kind: "error", message, detail });
      }
    }
  }

  return (
    <form onSubmit={onSubmit} style={{ padding: "18px", display: "grid", gap: "12px" }}>
      <div style={fieldStyle}>
        <span class="mono">entity</span>
        {entityId ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "12px",
              padding: "10px 14px",
              background: "var(--panel-2)",
              border: "1px solid var(--border)",
              borderRadius: "8px",
            }}
          >
            <span style={{ flex: 1 }}>{entityLabel}</span>
            <button
              type="button"
              style={linkBtnStyle}
              onClick={() => {
                setEntityId(null);
                setEntityLabel("");
              }}
            >
              Change
            </button>
          </div>
        ) : (
          <>
            <div style={{ display: "flex", gap: "8px" }}>
              <input
                class="search-input"
                type="text"
                value={entityQuery}
                onInput={(e) =>
                  setEntityQuery((e.target as HTMLInputElement).value)
                }
                placeholder="e.g. AAPL"
                aria-label="Search entities"
              />
              <button
                class="search-btn"
                type="submit"
                onClick={onSearch}
                disabled={search.kind === "loading"}
              >
                {search.kind === "loading" ? "Searching\u2026" : "Search"}
              </button>
            </div>
            {search.kind === "empty" ? (
              <p class="empty" style={{ padding: "8px 0" }}>
                {`No entities matched "${search.q}".`}
              </p>
            ) : null}
            {search.kind === "error" ? (
              <ErrorState message={search.message} detail={search.detail} />
            ) : null}
            {search.kind === "ok" ? (
              <ul class="entity-list">
                {search.entities.map((ent) => (
                  <li key={ent.id}>
                    <button
                      type="button"
                      onClick={() => pickEntity(ent)}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "16px",
                        width: "100%",
                        padding: "12px 16px",
                        background: "var(--panel-2)",
                        border: "1px solid var(--border)",
                        borderRadius: "8px",
                        color: "var(--text)",
                        cursor: "pointer",
                        textAlign: "left",
                        font: "inherit",
                      }}
                    >
                      <span class="entity-symbol">
                        {ent.symbol ?? "\u2014"}
                      </span>
                      <span class="entity-body">
                        <span class="entity-name">
                          {ent.name ?? "(unnamed)"}
                        </span>
                        <span class="entity-kind">{ent.kind}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        )}
      </div>

      <label style={fieldStyle}>
        <span class="mono">claim_type</span>
        <input
          class="search-input"
          type="text"
          value={claimType}
          onInput={(e) =>
            setClaimType((e.target as HTMLInputElement).value)
          }
          placeholder="e.g. price.close"
          aria-label="Claim type"
        />
      </label>

      <div style={fieldStyle}>
        <span class="mono">condition kind</span>
        <select
          class="search-input"
          style={{ height: "42px" }}
          value={form.kind}
          onChange={(e) =>
            patchForm({
              kind: (e.target as HTMLSelectElement).value as ConditionKind,
            })
          }
          aria-label="Condition kind"
        >
          {CONDITION_KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </div>

      {needsThreshold(form.kind) ? (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
          <label style={fieldStyle}>
            <span class="mono">threshold</span>
            <input
              class="search-input"
              type="number"
              step="any"
              value={form.threshold}
              onInput={(e) =>
                patchForm({
                  threshold: (e.target as HTMLInputElement).value,
                })
              }
              placeholder="e.g. 100"
              aria-label="Threshold"
            />
          </label>
          <label style={fieldStyle}>
            <span class="mono">field (defaults to value)</span>
            <input
              class="search-input"
              type="text"
              value={form.field}
              onInput={(e) =>
                patchForm({
                  field: (e.target as HTMLInputElement).value,
                })
              }
              placeholder="value"
              aria-label="Value field"
            />
          </label>
        </div>
      ) : null}

      {form.kind === "staleness_exceeds" ? (
        <label style={fieldStyle}>
          <span class="mono">seconds (must be positive)</span>
          <input
            class="search-input"
            type="number"
            step="any"
            min="0"
            value={form.seconds}
            onInput={(e) =>
              patchForm({
                seconds: (e.target as HTMLInputElement).value,
              })
            }
            placeholder="e.g. 86400"
            aria-label="Staleness seconds"
          />
        </label>
      ) : null}

      {form.kind === "contradiction" ? (
        <p class="muted" style={{ margin: 0 }}>
          Fires when two or more sources disagree on the same key and event
          date for this entity and claim type. No parameters.
        </p>
      ) : null}

      {create.kind === "error" ? (
        <ErrorState message={create.message} detail={create.detail} />
      ) : null}

      <div>
        <button
          class="search-btn"
          type="submit"
          disabled={create.kind === "creating"}
        >
          {create.kind === "creating" ? "Creating\u2026" : "Create alert"}
        </button>
      </div>

      {create.kind === "creating" ? <Loading label={"Creating\u2026"} /> : null}
    </form>
  );
}
