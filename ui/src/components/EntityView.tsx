import { useEffect, useState } from "preact/hooks";
import { useParams, useSearchParams } from "@neutron-build/core/client";
import {
  describeError,
  getClaims,
  getGaps,
  type ClaimsResponse,
  type GapsResponse,
} from "../lib/api";
import {
  ABSENT,
  describeFiling,
  formatMagnitude,
  formatNumber,
  formatPercent,
  getProfile,
  sparklinePath,
  toneFor,
  type EntityProfile,
} from "../lib/profile";
import { ClaimsTable } from "./ClaimsTable";
import { GapsList } from "./GapsList";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

type Async<T> =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; data: T }
  | { kind: "error"; message: string; detail?: string };

const SPARK_WIDTH = 640;
const SPARK_HEIGHT = 120;

function Metric(
  { label, value, context, tone }:
  { label: string; value: string; context?: string; tone?: string },
) {
  return (
    <article class="primary-metric">
      <span class="metric-kicker">{label}</span>
      <strong class={tone ? `value-${tone}` : undefined}>{value}</strong>
      {context ? <span class="metric-context">{context}</span> : null}
    </article>
  );
}

function PriceChart({ profile }: { profile: EntityProfile }) {
  const path = sparklinePath(profile.price.series, SPARK_WIDTH, SPARK_HEIGHT);
  if (path === null) {
    return (
      <p class="clean-empty">
        A price chart needs at least two observations that are not all identical.
      </p>
    );
  }
  const first = profile.price.series[0];
  const last = profile.price.series[profile.price.series.length - 1];
  const rising = last.close >= first.close;
  return (
    <figure class="entity-chart">
      <svg
        viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${profile.entity.symbol} close from ${first.date} to ${last.date}`}
      >
        <path d={path} class={rising ? "spark-rising" : "spark-falling"} />
      </svg>
      <figcaption>
        {profile.price.series.length} stored closes · {first.date} to {last.date}
        {profile.price.source ? ` · ${profile.price.source}` : ""}
      </figcaption>
    </figure>
  );
}

export function EntityView() {
  const params = useParams();
  const id = params.id;
  const [searchParams] = useSearchParams();
  const selectedType = searchParams.get("type");

  const [profile, setProfile] = useState<Async<EntityProfile>>({ kind: "idle" });
  const [gaps, setGaps] = useState<Async<GapsResponse>>({ kind: "idle" });
  const [claims, setClaims] = useState<Async<ClaimsResponse>>({ kind: "idle" });
  const [dataOpen, setDataOpen] = useState(false);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setProfile({ kind: "loading" });
    setGaps({ kind: "loading" });

    void (async () => {
      try {
        const data = await getProfile(id);
        if (!cancelled) setProfile({ kind: "ok", data });
      } catch (err) {
        if (!cancelled) {
          const { message, detail } = describeError(err);
          setProfile({ kind: "error", message, detail });
        }
      }
    })();

    void (async () => {
      try {
        const data = await getGaps(id);
        if (!cancelled) setGaps({ kind: "ok", data });
      } catch (err) {
        if (!cancelled) {
          const { message, detail } = describeError(err);
          setGaps({ kind: "error", message, detail });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [id]);

  useEffect(() => {
    if (!id || !dataOpen) return;
    let cancelled = false;
    setClaims({ kind: "loading" });
    void (async () => {
      try {
        const data = await getClaims(id, selectedType);
        if (!cancelled) setClaims({ kind: "ok", data });
      } catch (err) {
        if (!cancelled) {
          const { message, detail } = describeError(err);
          setClaims({ kind: "error", message, detail });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id, selectedType, dataOpen]);

  if (!id) return <Loading label="Loading entity…" />;
  if (profile.kind === "loading" || profile.kind === "idle") {
    return <Loading label="Measuring…" />;
  }
  if (profile.kind === "error") {
    return <ErrorState message={profile.message} detail={profile.detail} />;
  }

  const data = profile.data;
  const { entity, price, risk, derived } = data;
  const readableType = selectedType?.replaceAll("_", " ");
  const gapCount = gaps.kind === "ok" ? gaps.data.gaps.length : null;

  return (
    <div class="entity-view product-page">
      <a class="entity-back-link" href="/search">← Back to search</a>

      <header class="entity-page-heading">
        <div>
          <div class="entity-title-line">
            <h1>{entity.symbol}</h1>
            <span>{entity.kind.replaceAll("_", " ")}</span>
            {risk.risk_tier !== "unrated" ? (
              <span class={`risk-badge risk-badge-${risk.risk_tier}`}>{risk.risk_tier} risk</span>
            ) : null}
          </div>
          <p>{entity.name}</p>
        </div>
        <div class="entity-price-block">
          <strong>
            {price.latest === null ? ABSENT : `$${price.latest.toFixed(2)}`}
          </strong>
          <span class={`entity-price-change value-${toneFor(price.returns["30d"])}`}>
            {formatPercent(price.returns["30d"])} over 30 days
          </span>
          {price.as_of ? <small>Close of {price.as_of}</small> : null}
        </div>
      </header>

      <section class="primary-metrics" aria-label="Measured performance">
        <Metric
          label="90 days"
          value={formatPercent(price.returns["90d"])}
          tone={toneFor(price.returns["90d"])}
          context="trailing price return"
        />
        <Metric
          label="1 year"
          value={formatPercent(price.returns["365d"])}
          tone={toneFor(price.returns["365d"])}
          context="trailing price return"
        />
        <Metric
          label="Volatility"
          value={risk.volatility === null ? ABSENT : `${formatNumber(risk.volatility, 1)}%`}
          context="annualised, from stored closes"
        />
        <Metric
          label="Deepest fall"
          value={formatPercent(risk.max_drawdown)}
          tone={risk.max_drawdown === null ? undefined : "negative"}
          context="peak to trough over the stored window"
        />
      </section>

      {price.series.length > 0 ? (
        <section class="surface-card">
          <div class="section-heading">
            <div><p class="eyebrow">Price</p><h2>Stored history</h2></div>
            {risk.history_days ? <small>{risk.history_days} days</small> : null}
          </div>
          <PriceChart profile={data} />
        </section>
      ) : null}

      {data.fundamentals.length > 0 ? (
        <section class="surface-card">
          <div class="section-heading">
            <div><p class="eyebrow">Fundamentals</p><h2>Most recent filings</h2></div>
            <span class="count-badge">{data.fundamentals.length}</span>
          </div>
          <div class="entity-derived">
            <div>
              <span class="metric-kicker">Market value</span>
              <strong>{formatMagnitude(derived.market_cap, "USD")}</strong>
              <span class="metric-context">
                {derived.market_cap_as_of
                  ? `share count as filed ${derived.market_cap_as_of}`
                  : "not computable from stored filings"}
              </span>
            </div>
            <div>
              <span class="metric-kicker">Gross margin</span>
              <strong>{formatPercent(derived.gross_margin, 1)}</strong>
              <span class="metric-context">gross profit over revenue, same period</span>
            </div>
            <div>
              <span class="metric-kicker">Net margin</span>
              <strong class={`value-${toneFor(derived.net_margin)}`}>
                {formatPercent(derived.net_margin, 1)}
              </strong>
              <span class="metric-context">net income over revenue, same period</span>
            </div>
          </div>
          <div class="responsive-table">
            <table class="data-table">
              <thead>
                <tr><th>Measure</th><th>Value</th><th>Filing</th><th>Knowable from</th></tr>
              </thead>
              <tbody>
                {data.fundamentals.map((item) => (
                  <tr key={item.key}>
                    <td><strong>{item.label}</strong></td>
                    <td class={`value-${toneFor(item.value, item.higher_is_better)}`}>
                      {formatMagnitude(item.value, item.unit)}
                    </td>
                    <td><small>{describeFiling(item)}</small></td>
                    <td><small>{item.knowable_from}</small></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p class="research-note">
            Every figure is the most recently <em>knowable</em> filing, not the most recent
            fiscal period — a restatement filed later replaces the number it corrected.
            Ratios are only shown when both sides come from the same period.
          </p>
        </section>
      ) : null}

      <section class="surface-card">
        <div class="section-heading">
          <div><p class="eyebrow">Behaviour</p><h2>How it moves</h2></div>
        </div>
        <div class="entity-derived">
          <div>
            <span class="metric-kicker">Correlation to SPY</span>
            <strong>{formatNumber(risk.correlation_to_market)}</strong>
            <span class="metric-context">{risk.market_behavior.replaceAll("_", " ")}</span>
          </div>
          <div>
            <span class="metric-kicker">Risk-adjusted return</span>
            <strong>{formatNumber(risk.sharpe)}</strong>
            <span class="metric-context">annual return per unit of volatility</span>
          </div>
          <div>
            <span class="metric-kicker">Observations</span>
            <strong>{risk.sessions}</strong>
            <span class="metric-context">stored closes behind these figures</span>
          </div>
        </div>
      </section>

      {data.limits.length > 0 ? (
        <section class="surface-card entity-limits">
          <div class="section-heading">
            <div><p class="eyebrow">What is missing</p><h2>Not measured here</h2></div>
            <span class="count-badge count-warning">{data.limits.length}</span>
          </div>
          <ul>
            {data.limits.map((note) => <li key={note}>{note}</li>)}
          </ul>
        </section>
      ) : null}

      <button
        type="button"
        class="disclosure-button"
        aria-expanded={dataOpen}
        onClick={() => setDataOpen((open) => !open)}
      >
        <span>{dataOpen ? "Hide underlying data" : "View underlying data"}</span>
        <span aria-hidden="true">{dataOpen ? "−" : "+"}</span>
      </button>

      {dataOpen ? (
        <div class="detail-drawer">
          <section class="detail-block">
            <div class="section-heading">
              <div><p class="eyebrow">Coverage</p><h2>What is stored</h2></div>
            </div>
            <div class="responsive-table">
              <table class="data-table">
                <thead>
                  <tr><th>Type</th><th>Claims</th><th>Oldest event</th><th>Newest knowable</th></tr>
                </thead>
                <tbody>
                  {data.coverage.map((row) => (
                    <tr key={row.claim_type}>
                      <td><strong>{row.claim_type.replaceAll("_", " ")}</strong></td>
                      <td>{row.claims}</td>
                      <td><small>{row.oldest_event?.slice(0, 10) ?? ABSENT}</small></td>
                      <td><small>{row.newest?.slice(0, 10) ?? ABSENT}</small></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section class="detail-block">
            <div class="section-heading">
              <div>
                <p class="eyebrow">Measurements</p>
                <h2>{readableType ? `${readableType}` : "Most recent"}</h2>
              </div>
              {selectedType ? <a class="panel-clear" href={`/entity/${id}`}>all types</a> : null}
            </div>
            {claims.kind === "loading" || claims.kind === "idle" ? (
              <Loading label="Loading measurements…" />
            ) : null}
            {claims.kind === "error" ? (
              <ErrorState message={claims.message} detail={claims.detail} />
            ) : null}
            {claims.kind === "ok" ? <ClaimsTable claims={claims.data.claims} /> : null}
          </section>

          <section class="detail-block">
            <div class="section-heading">
              <div><p class="eyebrow">Coverage gaps</p><h2>Open gaps</h2></div>
              {gapCount !== null ? (
                <span class={`count-badge ${gapCount > 0 ? "count-warning" : ""}`}>{gapCount}</span>
              ) : null}
            </div>
            {gaps.kind === "loading" || gaps.kind === "idle" ? (
              <Loading label="Loading gaps…" />
            ) : null}
            {gaps.kind === "error" ? (
              <ErrorState message={gaps.message} detail={gaps.detail} />
            ) : null}
            {gaps.kind === "ok" ? <GapsList gaps={gaps.data.gaps} /> : null}
          </section>
        </div>
      ) : null}
    </div>
  );
}
