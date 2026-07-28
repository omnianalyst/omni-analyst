import { useState } from "preact/hooks";
import { describeError } from "../lib/api";
import {
  formatCost,
  parseNeeds,
  planObjective,
  runObjective,
  type ObjectivePlan,
  type ObjectiveRunResult,
} from "../lib/objective";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";
import { PlanDisplay } from "./PlanDisplay";

type PlanState =
  | { kind: "idle" }
  | { kind: "planning" }
  | { kind: "plan"; plan: ObjectivePlan }
  | { kind: "error"; message: string; detail?: string };

type RunState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; result: ObjectiveRunResult }
  | { kind: "error"; message: string; detail?: string };

const fieldStyle = { display: "grid", gap: "4px" } as const;

function formatOutput(output: unknown): string {
  if (output === null || output === undefined) return "";
  if (typeof output === "string") return output;
  return JSON.stringify(output, null, 2);
}

export function ObjectiveView() {
  const [text, setText] = useState("");
  const [target, setTarget] = useState("");
  const [needs, setNeeds] = useState("");
  const [entityKind, setEntityKind] = useState("");
  const [budget, setBudget] = useState("10");
  const [shareable, setShareable] = useState(false);

  const [plan, setPlan] = useState<PlanState>({ kind: "idle" });
  const [run, setRun] = useState<RunState>({ kind: "idle" });

  function buildRequest() {
    const parsedNeeds = parseNeeds(needs);
    const parsedBudget = Number(budget);
    return {
      text: text.trim(),
      target: target.trim(),
      needs: parsedNeeds,
      entity_kind: entityKind.trim() || null,
      shareable,
      budget: Number.isFinite(parsedBudget) ? parsedBudget : 10,
    };
  }

  async function onPlan(e: Event) {
    e.preventDefault();
    const req = buildRequest();
    if (!req.text || !req.target || req.needs.length === 0) {
      setPlan({
        kind: "error",
        message:
          "Objective text, target, and at least one claim type are required to plan.",
      });
      setRun({ kind: "idle" });
      return;
    }
    setPlan({ kind: "planning" });
    setRun({ kind: "idle" });
    try {
      const p = await planObjective(req);
      setPlan({ kind: "plan", plan: p });
    } catch (err) {
      const { message, detail } = describeError(err);
      setPlan({ kind: "error", message, detail });
    }
  }

  async function onRun(e: Event) {
    e.preventDefault();
    if (plan.kind !== "plan") return;
    const req = buildRequest();
    if (!req.text || !req.target || req.needs.length === 0) return;
    setRun({ kind: "running" });
    try {
      const result = await runObjective(req);
      setRun({ kind: "done", result });
    } catch (err) {
      const { message, detail } = describeError(err);
      setRun({ kind: "error", message, detail });
    }
  }

  const canRun = plan.kind === "plan";

  return (
    <div class="objective-view">
      <header class="page-head">
        <h1>Objective</h1>
        <p class="muted">
          State what you want to know. The system plans before it spends: you see
          the cost and the licence implications, then choose to run it.
        </p>
      </header>

      <section class="panel">
        <h2 class="panel-title">State the objective</h2>
        <form onSubmit={onPlan} style={{ padding: "18px", display: "grid", gap: "12px" }}>
          <label style={fieldStyle}>
            <span class="mono">objective text</span>
            <textarea
              class="search-input"
              style={{ height: "auto", minHeight: "64px", resize: "vertical" }}
              value={text}
              onInput={(e) =>
                setText((e.target as HTMLTextAreaElement).value)
              }
              placeholder="e.g. What is the fair value of this issuer relative to its peers?"
              aria-label="Objective text"
            />
          </label>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
            <label style={fieldStyle}>
              <span class="mono">target (symbol)</span>
              <input
                class="search-input"
                type="text"
                value={target}
                onInput={(e) => setTarget((e.target as HTMLInputElement).value)}
                placeholder="e.g. AAPL"
                aria-label="Target symbol"
              />
            </label>
            <label style={fieldStyle}>
              <span class="mono">entity kind (optional)</span>
              <input
                class="search-input"
                type="text"
                value={entityKind}
                onInput={(e) =>
                  setEntityKind((e.target as HTMLInputElement).value)
                }
                placeholder="e.g. issuer"
                aria-label="Entity kind"
              />
            </label>
          </div>
          <label style={fieldStyle}>
            <span class="mono">claim types needed (comma-separated)</span>
            <input
              class="search-input"
              type="text"
              value={needs}
              onInput={(e) => setNeeds((e.target as HTMLInputElement).value)}
              placeholder="e.g. price.close, market_cap, eps"
              aria-label="Claim types needed"
            />
          </label>
          <div style={{ display: "grid", gridTemplateColumns: "160px 1fr", gap: "12px", alignItems: "end" }}>
            <label style={fieldStyle}>
              <span class="mono">budget</span>
              <input
                class="search-input"
                type="number"
                step="0.5"
                min="0"
                value={budget}
                onInput={(e) => setBudget((e.target as HTMLInputElement).value)}
                aria-label="Budget"
              />
            </label>
            <label
              style={{
                display: "flex",
                gap: "8px",
                alignItems: "center",
                paddingBottom: "10px",
              }}
            >
              <input
                type="checkbox"
                checked={shareable}
                onInput={(e) =>
                  setShareable((e.target as HTMLInputElement).checked)
                }
              />
              <span class="mono">
                shareable answer &mdash; forbids licensed sources (the same
                objective plans differently)
              </span>
            </label>
          </div>
          <div>
            <button class="search-btn" type="submit" disabled={plan.kind === "planning"}>
              {plan.kind === "planning" ? "Planning\u2026" : "Plan objective"}
            </button>
          </div>
        </form>
      </section>

      {plan.kind === "planning" ? <Loading label="Planning\u2026" /> : null}
      {plan.kind === "error" ? (
        <ErrorState message={plan.message} detail={plan.detail} />
      ) : null}
      {plan.kind === "plan" ? <PlanDisplay plan={plan.plan} /> : null}

      {canRun ? (
        <section class="panel">
          <h2 class="panel-title">Run the plan</h2>
          <div style={{ padding: "18px" }}>
            <p class="muted" style={{ marginBottom: "12px" }}>
              Running executes the steps above and records anything it could not
              answer as demand. Total planned cost:{" "}
              <strong>{formatCost((plan as { plan: ObjectivePlan }).plan.cost)}</strong>.
            </p>
            <button
              class="search-btn"
              type="button"
              onClick={onRun}
              disabled={run.kind === "running"}
            >
              {run.kind === "running" ? "Running\u2026" : "Run objective"}
            </button>
          </div>
        </section>
      ) : null}

      {run.kind === "running" ? <Loading label="Running objective\u2026" /> : null}
      {run.kind === "error" ? (
        <ErrorState message={run.message} detail={run.detail} />
      ) : null}
      {run.kind === "done" ? <RunResults result={run.result} /> : null}
    </div>
  );
}

function RunResults({ result }: { result: ObjectiveRunResult }) {
  return (
    <section class="panel">
      <h2 class="panel-title">Run results</h2>
      <div class="gap-meta" style={{ padding: "14px 18px 0" }}>
        <span>
          answered <strong>{result.answered ? "yes" : "no"}</strong>
        </span>
        {result.demand_raised.length > 0 ? (
          <span>
            {result.demand_raised.length} demand gap
            {result.demand_raised.length === 1 ? "" : "s"} raised
          </span>
        ) : null}
      </div>
      {result.results.length > 0 ? (
        <ul class="gaps" style={{ marginTop: "8px" }}>
          {result.results.map((r) => {
            const out = formatOutput(r.output);
            return (
              <li class="gap-row" key={`${r.capability}|${r.claim_type}`}>
                <div class="gap-head">
                  <span class="gap-type">
                    {r.capability} &middot; {r.claim_type}
                  </span>
                  <span
                    class="gap-class"
                    style={r.ok ? {} : { color: "var(--tier-dead)" }}
                  >
                    {r.ok ? "ok" : "failed"}
                  </span>
                </div>
                {r.error ? <pre class="gap-detail">{r.error}</pre> : null}
                {out ? <pre class="gap-detail">{out}</pre> : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      {result.demand_raised.length > 0 ? (
        <div style={{ padding: "12px 18px" }}>
          <p class="mono" style={{ margin: "0 0 4px" }}>
            demand raised:
          </p>
          <ul style={{ margin: 0, paddingLeft: "20px" }}>
            {result.demand_raised.map((d) => (
              <li class="mono" key={d} style={{ color: "var(--muted)" }}>
                {d}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
