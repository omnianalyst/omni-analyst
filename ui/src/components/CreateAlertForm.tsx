import { useState } from "preact/hooks";
import { describeError, searchEntities, type Entity } from "../lib/api";
import { AuthRequiredError } from "../lib/auth";
import { getProfile } from "../lib/profile";
import {
  buildCondition,
  CONDITION_KINDS,
  conditionLabel,
  createAlert,
  defaultConditionForm,
  type ConditionFormState,
  type ConditionKind,
} from "../lib/alerts";
import { ErrorState } from "./ErrorState";
import { Hint } from "./Hint";
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

// The measured context for the chosen subject: what its latest covered
// price is, so a threshold is set against reality rather than remembered.
type PriceContext =
  | { kind: "loading" }
  | { kind: "none" }
  | { kind: "ok"; latest: number; asOf: string | null; symbol: string };

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
  const [priceContext, setPriceContext] = useState<PriceContext>({ kind: "none" });

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
    // Fetch the latest covered price so the threshold field carries context.
    // No coverage is an honest state too: the context line simply stays away
    // rather than implying a level the system cannot see.
    setPriceContext({ kind: "loading" });
    void getProfile(ent.id)
      .then((p) => {
        if (p.price.latest === null) setPriceContext({ kind: "none" });
        else
          setPriceContext({
            kind: "ok",
            latest: p.price.latest,
            asOf: p.price.as_of,
            symbol: ent.symbol ?? ent.name ?? "it",
          });
      })
      .catch(() => setPriceContext({ kind: "none" }));
  }

  async function onSubmit(e: Event) {
    e.preventDefault();
    if (create.kind === "creating") return;
    if (!entityId) {
      setCreate({
        kind: "error",
        message: "Choose a subject for this alert to watch.",
      });
      return;
    }
    const ct = claimType.trim();
    if (!ct) {
      setCreate({
        kind: "error",
        message: "Name which piece of information to watch.",
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
      setPriceContext({ kind: "none" });
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
        <span class="field-label">Subject to watch</span>
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
                setPriceContext({ kind: "none" });
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
        <span class="field-label">Which piece of information</span>
        <input
          class="search-input"
          type="text"
          value={claimType}
          onInput={(e) =>
            setClaimType((e.target as HTMLInputElement).value)
          }
          placeholder="e.g. price_snapshot"
          aria-label="Which piece of information"
        />
        <span class="field-help">
          The kind of <Hint term="claim">claim</Hint> to watch on this subject.
        </span>
      </label>

      <div style={fieldStyle}>
        <span class="field-label">Alert me when</span>
        <select
          class="search-input"
          style={{ height: "42px" }}
          value={form.kind}
          onChange={(e) =>
            patchForm({
              kind: (e.target as HTMLSelectElement).value as ConditionKind,
            })
          }
          aria-label="Alert me when"
        >
          {CONDITION_KINDS.map((k) => (
            <option key={k} value={k}>
              {conditionLabel(k)}
            </option>
          ))}
        </select>
      </div>

      {needsThreshold(form.kind) ? (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
          <label style={fieldStyle}>
            <span class="field-label">The level</span>
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
              aria-label="The level"
            />
            {priceContext.kind === "ok" ? (
              <span class="field-help">
                {priceContext.symbol} last covered at $
                {priceContext.latest.toLocaleString(undefined, {
                  maximumFractionDigits: 2,
                })}
                {priceContext.asOf
                  ? ` (close of ${priceContext.asOf.slice(0, 10)})`
                  : ""}
                .
              </span>
            ) : priceContext.kind === "loading" ? (
              <span class="field-help">Reading latest coverage…</span>
            ) : null}
          </label>
          <label style={fieldStyle}>
            <span class="field-label">Which number to compare (optional)</span>
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
              aria-label="Which number to compare"
            />
            <span class="field-help">
              Claims can carry several numbers. Leave blank for the main one.
            </span>
          </label>
        </div>
      ) : null}

      {form.kind === "staleness_exceeds" ? (
        <label style={fieldStyle}>
          <span class="field-label">After how long, in seconds</span>
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
            aria-label="After how long, in seconds"
          />
          <span class="field-help">
            86400 is a day, 604800 a week. Fires when nothing newer has arrived
            in that time.
          </span>
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
