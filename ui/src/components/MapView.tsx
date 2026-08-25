import { useEffect, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError, request } from "../lib/api";
import { AssetMetric } from "./ScannerView";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

// The market as pyramids around a single point. The center is the best
// measured asset overall; four arms radiate out -- stocks above, defensive
// below, crypto to the left, companies to the right -- and each arm widens
// with rank: the innermost row holds the top name, deeper rows hold more.
// Placement is ordinal (rank), never a invented distance scale; the measured
// numbers travel on the chip (hover) and the linking table carries the
// precision. Same /scanner/market payload the ranked tables read.

type AssetClass = "stocks" | "crypto" | "defensive";

interface SectorLeader {
  symbol: string;
  name: string;
  return_window: number;
  as_of: string;
}

interface SectorGroup {
  name: string;
  symbol: string;
  coverage: number;
  leaders: SectorLeader[];
}

interface MapData {
  category_rankings: Record<AssetClass, AssetMetric[]>;
  sectors: SectorGroup[];
  sector_coverage: { available: number; total: number; window_sessions: number };
  as_of: string;
}

type State =
  | { kind: "loading" }
  | { kind: "ok"; data: MapData }
  | { kind: "error"; message: string; detail?: string };

const SECTOR_COLORS = [
  "#5eead4", "#93c5fd", "#fca5a5", "#fcd34d", "#c4b5fd", "#86efac",
  "#f9a8d4", "#7dd3fc", "#fdba74", "#a3e635", "#f0abfc",
];

const ARM_TITLES: Record<AssetClass, string> = {
  stocks: "Stocks & ETFs",
  defensive: "Defensive & real assets",
  crypto: "Crypto",
};

// Triangular packing with a merge rule: if fewer items remain than the next
// row would hold, they join the current row, so seven names sit as 1-2-4
// rather than 1-2-3-1.
function pyramidRows<T>(items: T[], maxRows = 6): T[][] {
  const rows: T[][] = [];
  let index = 0;
  for (let size = 1; size <= maxRows && index < items.length; size += 1) {
    const next: T[] = items.slice(index, index + size);
    index += size;
    const remaining = items.length - index;
    if (remaining > 0 && remaining < size + 1) {
      next.push(...items.slice(index, index + remaining));
      index += remaining;
    }
    rows.push(next);
  }
  return rows;
}

function assetFacts(asset: AssetMetric): string {
  const parts = [
    `${asset.name} (${asset.area})`,
    asset.median_annual_return != null
      ? `median year ${asset.median_annual_return.toFixed(1)}%`
      : "no median yet",
    asset.volatility != null ? `volatility ${asset.volatility.toFixed(1)}%` : null,
  ].filter(Boolean) as string[];
  const scores = asset.scores;
  const measured: Array<[string, number | null | undefined]> = [
    ["growth", scores.durable_growth],
    ["consistency", scores.consistency],
    ["stability", scores.stability],
    ["downside", scores.downside],
    ["diversification", scores.diversification],
  ];
  const present = measured.filter(([, v]) => typeof v === "number") as Array<[string, number]>;
  if (present.length >= 2) {
    present.sort((a, b) => a[1] - b[1]);
    parts.push(`weakest: ${present[0][0]} ${present[0][1].toFixed(0)}`);
  }
  return parts.join(" · ");
}

function AssetChip({ asset, rank }: { asset: AssetMetric; rank: number }) {
  return (
    <a
      class="map-chip"
      href={`/search?q=${encodeURIComponent(asset.symbol)}`}
      title={assetFacts(asset)}
    >
      <small>{rank}</small>
      {asset.symbol}
    </a>
  );
}

// Up and down arms: horizontal rows of chips, widest row furthest from the
// center. A vertical arm of rows reads as a pyramid; a horizontal arm would
// need rotated text, which is unreadable, so the sideways arms below pack
// columns instead.
function VerticalPyramid({
  title,
  assets,
  direction,
}: {
  title: string;
  assets: AssetMetric[];
  direction: "up" | "down";
}) {
  const eligible = assets.filter(
    (a) => a.scores.evidence_complete !== false && typeof a.scores.balanced === "number",
  );
  const ordered = [...eligible].sort(
    (a, b) => (b.scores.balanced ?? -Infinity) - (a.scores.balanced ?? -Infinity),
  );
  const shown = ordered.slice(0, 21);
  const rows = pyramidRows(shown);
  const incomplete = assets.length - eligible.length;
  if (direction === "down") rows.reverse();
  return (
    <div class={`map-arm map-arm-${direction}`}>
      <p class="map-arm-title">{title}</p>
      {ordered.length === 0 ? (
        <p class="map-arm-empty">Nothing fully measured yet.</p>
      ) : (
        <div class="map-rows">
          {rows.map((row) => (
            <div class="map-row">
              {row.map((asset) => (
                <AssetChip key={asset.symbol} asset={asset} rank={shown.indexOf(asset) + 1} />
              ))}
            </div>
          ))}
        </div>
      )}
      {ordered.length > shown.length || incomplete > 0 ? (
        <p class="map-arm-note">
          {ordered.length > shown.length ? `+${ordered.length - shown.length} more ranked ` : ""}
          {incomplete > 0 ? `${incomplete} still short of full evidence` : ""}
          <a href="/search">on Discover</a>
        </p>
      ) : null}
    </div>
  );
}

// Sideways pyramid (crypto, left arm): columns of chips, the nearest column
// to the center holds the top name, further columns hold more. All text
// stays horizontal because the shape widens vertically.
function SidewaysPyramid({ title, assets }: { title: string; assets: AssetMetric[] }) {
  const eligible = assets.filter(
    (a) => a.scores.evidence_complete !== false && typeof a.scores.balanced === "number",
  );
  const ordered = [...eligible].sort(
    (a, b) => (b.scores.balanced ?? -Infinity) - (a.scores.balanced ?? -Infinity),
  );
  const shown = ordered.slice(0, 12);
  const columns = pyramidRows(shown, 4);
  return (
    <div class="map-arm map-arm-left">
      <p class="map-arm-title">{title}</p>
      {ordered.length === 0 ? (
        <p class="map-arm-empty">Nothing fully measured yet.</p>
      ) : (
        <div class="map-columns">
          {columns.map((column) => (
            <div class="map-column">
              {column.map((asset) => (
                <AssetChip key={asset.symbol} asset={asset} rank={shown.indexOf(asset) + 1} />
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Companies, right arm: one strip per measured sector, ordered by the
// sector's own leader. The strip's first chip -- its apex -- touches the
// center spine; further leaders extend outward.
function SectorSpine({ data }: { data: MapData }) {
  const groups = [...data.sectors].sort(
    (a, b) =>
      (b.leaders[0]?.return_window ?? -Infinity) -
      (a.leaders[0]?.return_window ?? -Infinity),
  );
  return (
    <div class="map-arm map-arm-right">
      <p class="map-arm-title">
        Companies by sector · {data.sector_coverage.window_sessions}-session return
      </p>
      {groups.length === 0 ? (
        <p class="map-arm-empty">
          Company rankings are still building; no sector has comparable history yet.
        </p>
      ) : (
        <div class="map-sector-list">
          {groups.map((sector, index) => (
            <div
              class="map-sector-strip"
              key={sector.symbol}
              style={`border-left-color:${SECTOR_COLORS[index % SECTOR_COLORS.length]}`}
            >
              <span class="map-sector-label" title={sector.name}>
                {sector.symbol}
              </span>
              {sector.leaders.map((leader) => (
                <a
                  class="map-chip map-chip-company"
                  key={leader.symbol}
                  href={`/search?q=${encodeURIComponent(leader.symbol)}`}
                  title={`${leader.name} · ${sector.name} · ${data.sector_coverage.window_sessions}-session ${leader.return_window.toFixed(1)}%`}
                >
                  {leader.symbol}
                </a>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function MapView() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    request<MapData>("/scanner/market", authHeaderIfPresent())
      .then((data) => { if (!cancelled) setState({ kind: "ok", data }); })
      .catch((error) => {
        if (cancelled) return;
        const described = describeError(error);
        setState({ kind: "error", message: described.message, detail: described.detail });
      });
    return () => { cancelled = true; };
  }, []);

  if (state.kind === "loading") return <Loading label="Placing the measured market…" />;
  if (state.kind === "error") return <ErrorState message={state.message} detail={state.detail} />;

  const { data } = state;
  const universe = (["stocks", "defensive", "crypto"] as AssetClass[])
    .flatMap((cls) => data.category_rankings[cls] ?? [])
    .filter(
      (a) => a.scores.evidence_complete !== false && typeof a.scores.balanced === "number",
    )
    .sort((a, b) => (b.scores.balanced ?? -Infinity) - (a.scores.balanced ?? -Infinity));
  const best = universe[0] ?? null;

  return (
    <div class="map-view product-page">
      <header class="map-heading">
        <div>
          <h1>Map</h1>
          <p>
            Every measured asset as pyramids around the best point. Innermost row = highest
            balanced score; rows widen with rank. Companies are ranked within sector by{" "}
            {data.sector_coverage.window_sessions}-session return. Hover a name for its
            measured facts; the ranked tables carry the precision.
          </p>
        </div>
        <div class="discover-compact-meta">
          <a class="btn-secondary compact-button" href="/search">Tables</a>
          <time dateTime={data.as_of}>Updated {new Date(data.as_of).toLocaleString()}</time>
        </div>
      </header>

      <div class="map-canvas" role="img" aria-label="The measured market arranged as pyramids around the single best point">
        <VerticalPyramid
          title={ARM_TITLES.stocks}
          assets={data.category_rankings.stocks ?? []}
          direction="up"
        />
        <div class="map-mid">
          <SidewaysPyramid title={ARM_TITLES.crypto} assets={data.category_rankings.crypto ?? []} />
          <div class="map-center">
            {best ? (
              <>
                <span class="map-center-dot" aria-hidden="true" />
                <a
                  class="map-center-symbol"
                  href={`/search?q=${encodeURIComponent(best.symbol)}`}
                  title={assetFacts(best)}
                >
                  {best.symbol}
                </a>
                <small>best measured · balanced {best.scores.balanced?.toFixed(0)}</small>
              </>
            ) : (
              <small>nothing fully measured yet</small>
            )}
          </div>
          <SectorSpine data={data} />
        </div>
        <VerticalPyramid
          title={ARM_TITLES.defensive}
          assets={data.category_rankings.defensive ?? []}
          direction="down"
        />
      </div>
    </div>
  );
}
