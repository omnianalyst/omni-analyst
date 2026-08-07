import { useEffect, useState } from "preact/hooks";
import { describeError } from "../lib/api";
import {
  fetchClaimTypes,
  formatCost,
  humaniseClaimType,
  parseNeeds,
  planObjective,
  runObjective,
  type ObjectivePlan,
  type ObjectiveRunResult,
} from "../lib/objective";
import { ErrorState } from "./ErrorState";
import { Hint } from "./Hint";
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
  // The claim types the planner can actually produce. Typing one from memory
  // into a free-text box was the old design: a silent typo came back as
  // "no capability produces this", indistinguishable from a real shortfall.
  const [available, setAvailable] = useState<string[]>([]);

  const [plan, setPlan] = useState<PlanState>({ kind: "idle" });
  const [run, setRun] = useState<RunState>({ kind: "idle" });

  useEffect(() => {
    let cancelled = false;
    fetchClaimTypes()
      .then((types) => {
        if (!cancelled) setAvailable(types);
      })
      .catch(() => {
        // The picker is an aid, not a gate. If the registry is unreachable the
        // field still accepts a typed value and the planner still answers.
        if (!cancelled) setAvailable([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const selected = parseNeeds(needs);

  function toggleNeed(claimType: string) {
    const next = selected.includes(claimType)
      ? selected.filter((n) => n !== claimType)
      : [...selected, claimType];
    setNeeds(next.join(", "));
  }

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
          "A question, a subject, and at least one kind of information are all needed before a plan can be made.",
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
        <h1>Ask</h1>
        <p class="muted">
          State what you want to know. The system plans before it spends: you see
          what it would cost and which sources it would touch, then decide
          whether to run it.
        </p>
      </header>

      <section class="panel">
        <h2 class="panel-title">What do you want to know?</h2>
        <form onSubmit={onPlan} style={{ padding: "18px", display: "grid", gap: "16px" }}>
          <label style={fieldStyle}>
            <span class="field-label">Your question</span>
            <textarea
              class="search-input"
              style={{ height: "auto", minHeight: "64px", resize: "vertical" }}
              value={text}
              onInput={(e) =>
                setText((e.target as HTMLTextAreaElement).value)
              }
              placeholder="e.g. What is the fair value of this issuer relative to its peers?"
              aria-label="Your question"
            />
          </label>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
            <label style={fieldStyle}>
              <span class="field-label">Subject</span>
              <input
                class="search-input"
                type="text"
                value={target}
                onInput={(e) => setTarget((e.target as HTMLInputElement).value)}
                placeholder="e.g. AAPL"
                aria-label="Subject"
              />
              <span class="field-help">The ticker or name you are asking about.</span>
            </label>
            <label style={fieldStyle}>
              <span class="field-label">Kind of subject (optional)</span>
              <input
                class="search-input"
                type="text"
                value={entityKind}
                onInput={(e) =>
                  setEntityKind((e.target as HTMLInputElement).value)
                }
                placeholder="e.g. company"
                aria-label="Kind of subject"
              />
              <span class="field-help">
                Narrows the search when one symbol matches more than one thing.
              </span>
            </label>
          </div>

          <div style={fieldStyle}>
            <span class="field-label">
              What information does answering it need?
            </span>
            {available.length > 0 ? (
              <div class="chip-set" role="group" aria-label="Information needed">
                {available.map((t) => (
                  <button
                    key={t}
                    type="button"
                    class={`chip ${selected.includes(t) ? "chip-on" : ""}`}
                    aria-pressed={selected.includes(t)}
                    onClick={() => toggleNeed(t)}
                  >
                    {humaniseClaimType(t)}
                  </button>
                ))}
              </div>
            ) : (
              <input
                class="search-input"
                type="text"
                value={needs}
                onInput={(e) => setNeeds((e.target as HTMLInputElement).value)}
                placeholder="e.g. price_snapshot, fundamental_metric"
                aria-label="Information needed"
              />
            )}
            <span class="field-help">
              Only what the system can currently fetch is listed. Pick everything
              the answer depends on.
            </span>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "180px 1fr", gap: "16px", alignItems: "start" }}>
            <label style={fieldStyle}>
              <span class="field-label">Spending limit</span>
              <input
                class="search-input"
                type="number"
                step="0.5"
                min="0"
                value={budget}
                onInput={(e) => setBudget((e.target as HTMLInputElement).value)}
                aria-label="Spending limit"
              />
              <span class="field-help">
                In credits. A step that would exceed it is left out of the plan.
              </span>
            </label>
            <label style={fieldStyle}>
              <span
                style={{ display: "flex", gap: "8px", alignItems: "center" }}
              >
                <input
                  type="checkbox"
                  checked={shareable}
                  onInput={(e) =>
                    setShareable((e.target as HTMLInputElement).checked)
                  }
                />
                <span class="field-label" style={{ margin: 0 }}>
                  Answer must be shareable
                </span>
              </span>
              <span class="field-help">
                Excludes sources whose terms forbid passing the data on, so the
                answer can be shown to someone else. The same question plans
                differently \u2014 and may not be answerable at all.
              </span>
            </label>
          </div>
          <div>
            <button class="search-btn" type="submit" disabled={plan.kind === "planning"}>
              {plan.kind === "planning" ? "Planning\u2026" : "Plan this"}
            </button>
          </div>
        </form>
      </section>

      {plan.kind === "planning" ? <Loading label="Planning…" /> : null}
      {plan.kind === "error" ? (
        <ErrorState message={plan.message} detail={plan.detail} />
      ) : null}
      {plan.kind === "plan" ? <PlanDisplay plan={plan.plan} /> : null}

      {canRun ? (
        <section class="panel">
          <h2 class="panel-title">Run it</h2>
          <div style={{ padding: "18px" }}>
            <p class="muted" style={{ marginBottom: "12px" }}>
              This carries out the steps above. Anything it cannot answer is
              recorded as{" "}
              <Hint term="demand">standing demand</Hint>, so the system keeps
              trying in the background. Planned cost:{" "}
              <strong>{formatCost((plan as { plan: ObjectivePlan }).plan.cost)}</strong>.
            </p>
            <button
              class="search-btn"
              type="button"
              onClick={onRun}
              disabled={run.kind === "running"}
            >
              {run.kind === "running" ? "Running\u2026" : "Run it"}
            </button>
          </div>
        </section>
      ) : null}

      {run.kind === "running" ? <Loading label="Running objective…" /> : null}
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
