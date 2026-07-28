import { useEffect, useState } from "preact/hooks";
import { useParams } from "@neutron-build/core/client";
import {
  describeError,
  getCoverage,
  getGaps,
  type CoverageResponse,
  type GapsResponse,
} from "../lib/api";
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
  const [coverage, setCoverage] = useState<Async<CoverageResponse>>({
    kind: "idle",
  });
  const [gaps, setGaps] = useState<Async<GapsResponse>>({ kind: "idle" });

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

  return (
    <div class="entity-view">
      <header class="page-head">
        <p class="muted mono">entity {id}</p>
        <h1>Coverage</h1>
      </header>

      <section class="panel">
        <h2 class="panel-title">Claims by type</h2>
        {coverage.kind === "loading" || coverage.kind === "idle" ? (
          <Loading label="Loading coverage…" />
        ) : null}
        {coverage.kind === "error" && (
          <ErrorState message={coverage.message} detail={coverage.detail} />
        )}
        {coverage.kind === "ok" && <CoverageTable groups={coverage.data.groups} />}
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
  );
}
