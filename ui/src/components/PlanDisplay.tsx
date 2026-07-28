import {
  explainLicenceTier,
  explainShortfall,
  formatCost,
  type ObjectivePlan,
} from "../lib/objective";

export function PlanDisplay({ plan }: { plan: ObjectivePlan }) {
  return (
    <div class="plan">
      <p class="plan-status">
        {plan.satisfiable ? (
          <span class="ok">
            Fully answerable within budget and licence. Review the cost before
            running.
          </span>
        ) : null}
        {plan.partial ? (
          <span class="warn">
            Partially answerable. Some needs cannot be met; they are written
            back as demand if you run this.
          </span>
        ) : null}
        {!plan.satisfiable && !plan.partial ? (
          <span class="warn">
            This objective cannot be answered as stated. Running it records the
            gaps as demand rather than producing an answer.
          </span>
        ) : null}
      </p>

      {plan.steps.length > 0 ? (
        <section class="panel">
          <h2 class="panel-title">
            Steps the system would run &middot; {formatCost(plan.cost)} total
          </h2>
          <table class="coverage">
            <thead>
              <tr>
                <th>Capability</th>
                <th>Claim type</th>
                <th>Target</th>
                <th class="num">Cost</th>
                <th>Licence tier</th>
              </tr>
            </thead>
            <tbody>
              {plan.steps.map((s) => (
                <tr key={`${s.capability}|${s.claim_type}`}>
                  <td class="claim-type">{s.capability}</td>
                  <td>{s.claim_type}</td>
                  <td class="mono">{s.target}</td>
                  <td class="num">{formatCost(s.cost)}</td>
                  <td>
                    <span class={s.licence_tier === "private" ? "byo" : "faint"}>
                      {explainLicenceTier(s.licence_tier)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      {plan.shortfalls.length > 0 ? (
        <section class="panel">
          <h2 class="panel-title">
            Shortfalls &mdash; what cannot be answered, and why
          </h2>
          <ul class="gaps">
            {plan.shortfalls.map((f) => (
              <li class="gap-row" key={`${f.claim_type}|${f.reason}`}>
                <div class="gap-head">
                  <span class="gap-type">{f.claim_type}</span>
                </div>
                <p class="shortfall-reason">{explainShortfall(f.reason)}</p>
                <pre class="gap-detail">{f.detail}</pre>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <details>
        <summary class="mono">Planner summary</summary>
        <pre class="gap-detail">{plan.summary}</pre>
      </details>
    </div>
  );
}
