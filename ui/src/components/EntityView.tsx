import { useEffect, useState } from "preact/hooks";
import { useParams, useSearchParams } from "@neutron-build/core/client";
import {
  describeError,
  getClaims,
  getCoverage,
  getGaps,
  type ClaimsResponse,
  type CoverageResponse,
  type GapsResponse,
} from "../lib/api";
import { contradictionTypes } from "../lib/gaps";
import { ClaimsTable } from "./ClaimsTable";
import { CoverageTable } from "./CoverageTable";
import { GapsList } from "./GapsList";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

type Async<T> =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; data: T }
  | { kind: "error"; message: string; detail?: string };

export function EntityView() {
  const params = useParams();
  const id = params.id;
  const [searchParams] = useSearchParams();
  const selectedType = searchParams.get("type");
  const [coverage, setCoverage] = useState<Async<CoverageResponse>>({
    kind: "idle",
  });
  const [gaps, setGaps] = useState<Async<GapsResponse>>({ kind: "idle" });
  const [claims, setClaims] = useState<Async<ClaimsResponse>>({ kind: "idle" });

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setCoverage({ kind: "loading" });
    setGaps({ kind: "loading" });

    void (async () => {
      try {
        const data = await getCoverage(id);
        if (!cancelled) setCoverage({ kind: "ok", data });
      } catch (err) {
        if (!cancelled) {
          const { message, detail } = describeError(err);
          setCoverage({ kind: "error", message, detail });
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
    if (!id) {
      setClaims({ kind: "idle" });
      return;
    }
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
  }, [id, selectedType]);

  const conflictTypes =
    gaps.kind === "ok" ? contradictionTypes(gaps.data.gaps) : new Set<string>();

  if (!id) {
    return (
      <div class="entity-view">
        <header class="page-head">
          <p class="muted mono">entity …</p>
          <h1>Coverage</h1>
        </header>
        <section class="panel">
          <h2 class="panel-title">Claims by type</h2>
          <Loading label="Loading entity…" />
        </section>
      </div>
    );
  }

  const entity = coverage.kind === "ok" ? coverage.data.entity : null;
  const claimCount = coverage.kind === "ok"
    ? coverage.data.groups.reduce((total, group) => total + group.count, 0)
    : null;
  const gapCount = gaps.kind === "ok" ? gaps.data.gaps.length : null;
  const readableType = selectedType?.replaceAll("_", " ");

  return (
    <div class="entity-view product-page">
      <a class="entity-back-link" href="/search?tab=search">← Back to search</a>
      <header class="entity-page-heading">
        <div>
          <div class="entity-title-line">
            <h1>{entity?.symbol ?? "Entity"}</h1>
            {entity ? <span>{entity.kind.replaceAll("_", " ")}</span> : null}
          </div>
          <p>{entity?.name ?? "Loading company details…"}</p>
        </div>
        <div class="entity-summary-strip">
          <span><strong>{coverage.kind === "ok" ? coverage.data.groups.length : "—"}</strong> data types</span>
          <span><strong>{claimCount ?? "—"}</strong> measurements</span>
          <span class={gapCount !== null && gapCount > 0 ? "value-negative" : ""}>
            <strong>{gapCount ?? "—"}</strong> open gaps
          </span>
        </div>
      </header>

      <div class="entity-grid">
        <section class="panel entity-coverage">
          <h2 class="panel-title">Available data</h2>
          {coverage.kind === "loading" || coverage.kind === "idle" ? (
            <Loading label="Loading coverage…" />
          ) : null}
          {coverage.kind === "error" && (
            <ErrorState message={coverage.message} detail={coverage.detail} />
          )}
          {coverage.kind === "ok" && (
            <CoverageTable
              groups={coverage.data.groups}
              entityId={id}
              selectedType={selectedType}
              contradictionTypes={conflictTypes}
            />
          )}
        </section>

        <div class="entity-detail">
          <section class="panel">
            <h2 class="panel-title">
              {readableType ? `${readableType} measurements` : "Recent measurements"}
              {selectedType ? (
                <a class="panel-clear" href={`/entity/${id}`}>
                  all types
                </a>
              ) : null}
            </h2>
            {claims.kind === "loading" || claims.kind === "idle" ? (
              <Loading label={`Loading ${readableType ?? "recent"} measurements…`} />
            ) : null}
            {claims.kind === "error" && (
              <ErrorState message={claims.message} detail={claims.detail} />
            )}
            {claims.kind === "ok" && (
              <ClaimsTable claims={claims.data.claims} />
            )}
          </section>

          <section class="panel">
            <h2 class="panel-title">Open gaps</h2>
            {gaps.kind === "loading" || gaps.kind === "idle" ? (
              <Loading label="Loading gaps…" />
            ) : null}
            {gaps.kind === "error" && (
              <ErrorState message={gaps.message} detail={gaps.detail} />
            )}
            {gaps.kind === "ok" && <GapsList gaps={gaps.data.gaps} />}
          </section>
        </div>
      </div>
    </div>
  );
}
