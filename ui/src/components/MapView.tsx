import { useEffect, useMemo, useState } from "preact/hooks";
import { authHeaderIfPresent, describeError, request } from "../lib/api";
import { AssetMetric } from "./ScannerView";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

// The market as triangles converging on one point. Every group -- the three
// asset classes and each measured company sector -- owns a triangular wedge
// whose apex touches the shared center; the group's best name sits nearest
// the center and deeper ranks sit further out along the widening wedge, so
// each wedge literally is that group's pyramid. Placement is ordinal (rank),
// never an invented distance scale; measured facts travel on hover. Same
// /scanner/market payload the ranked tables read.

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

interface Chip {
  id: string;
  label: string;
  href: string;
  title: string;
  rank: number;
  width: number;
  x: number;
  y: number;
}

interface Wedge {
  key: string;
  kind: "class" | "sector";
  label: string;
  sectorName?: string;
  color: string;
  chips: Chip[];
  a0: number;
  a1: number;
  mid: number;
  pathR: number;
  labelR: number;
}

const SIZE = 1000;
const C = SIZE / 2;
const INNER_R = 104;
const STEP = 56;
const SLOT = 76;
const CHIP_H = 26;
const MAX_SECTOR_LEADERS = 5;
const CLASS_CHIP_LIMIT = 15;

const CLASS_COLORS: Record<AssetClass, string> = {
  stocks: "#7dd3fc",
  defensive: "#fbbf24",
  crypto: "#5eead4",
};

const SECTOR_COLORS = [
  "#93c5fd", "#fca5a5", "#c4b5fd", "#86efac", "#f9a8d4",
  "#7dd3fc", "#fdba74", "#a3e635", "#f0abfc", "#fcd34d", "#5eead4",
];

// The plain-English name under each sector ticker, so nobody needs to know
// that XLB is Materials. The sector's own name arrives from the payload;
// this maps the sector ETF ticker to the everyday word.
const SECTOR_PLAIN: Record<string, string> = {
  XLK: "Tech",
  XLF: "Banks & money",
  XLV: "Health care",
  XLE: "Energy",
  XLY: "Shopping",
  XLP: "Everyday goods",
  XLI: "Industry",
  XLU: "Utilities",
  XLB: "Raw materials",
  XLC: "Phone & media",
  XLRE: "Real estate",
};

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

function chipWidth(label: string, rank: number): number {
  return 12 + (rank > 0 ? 13 : 0) + label.length * 7.4 + 12;
}

function buildWedges(data: MapData): Wedge[] {
  const groups: Array<Wedge> = [];
  const classLabels: Record<AssetClass, string> = {
    stocks: "Stocks & ETFs",
    defensive: "Defensive & real assets",
    crypto: "Crypto",
  };
  for (const cls of ["stocks", "defensive", "crypto"] as AssetClass[]) {
    const eligible = (data.category_rankings[cls] ?? []).filter(
      (a) => a.scores.evidence_complete !== false && typeof a.scores.balanced === "number",
    );
    const ordered = [...eligible].sort(
      (a, b) => (b.scores.balanced ?? -Infinity) - (a.scores.balanced ?? -Infinity),
    );
    const chips = ordered.slice(0, CLASS_CHIP_LIMIT).map((asset, index) => ({
      id: asset.symbol,
      label: asset.symbol,
      href: `/search?q=${encodeURIComponent(asset.symbol)}`,
      title: assetFacts(asset),
      rank: index + 1,
      width: chipWidth(asset.symbol, index + 1),
      x: 0,
      y: 0,
    }));
    if (chips.length > 0) {
      groups.push({
        key: cls,
        kind: "class",
        label: classLabels[cls],
        color: CLASS_COLORS[cls],
        chips,
        a0: 0,
        a1: 0,
        mid: 0,
        pathR: 0,
        labelR: 0,
      });
    }
  }
  const sectors = [...data.sectors].sort(
    (a, b) =>
      (b.leaders[0]?.return_window ?? -Infinity) -
      (a.leaders[0]?.return_window ?? -Infinity),
  );
  sectors.forEach((sector, index) => {
    const leaders = sector.leaders.slice(0, MAX_SECTOR_LEADERS);
    if (leaders.length === 0) return;
    const chips = leaders.map((leader, seat) => ({
      id: leader.symbol,
      label: leader.symbol,
      href: `/search?q=${encodeURIComponent(leader.symbol)}`,
      title: `${leader.name} · ${sector.name} · ${data.sector_coverage.window_sessions}-session ${leader.return_window.toFixed(1)}%`,
      rank: seat + 1,
      width: chipWidth(leader.symbol, seat + 1),
      x: 0,
      y: 0,
    }));
    groups.push({
      key: sector.symbol,
      kind: "sector",
      label: sector.symbol,
      sectorName: sector.name.split(" ").slice(0, 2).join(" "),
      color: SECTOR_COLORS[index % SECTOR_COLORS.length],
      chips,
      a0: 0,
      a1: 0,
      mid: 0,
      pathR: 0,
      labelR: 0,
    });
  });

  // Wider groups take wider wedges, but sub-linearly: a 15-name class does
  // not need fifteen times the arc of a 5-name sector, and angle is what
  // lets outer bands hold more chips -- the pyramid's widening rows.
  const total = groups.reduce((sum, g) => sum + Math.pow(g.chips.length, 0.7), 0);
  let cursor = -Math.PI / 2;
  for (const group of groups) {
    const span = (Math.pow(group.chips.length, 0.7) / total) * Math.PI * 2;
    group.a0 = cursor;
    group.a1 = cursor + span;
    group.mid = cursor + span / 2;
    cursor += span;

    // Fill bands from the center outward. A band's capacity grows with its
    // radius (more arc to sit on), so inner bands hold one chip and outer
    // bands hold more -- rank order becomes geometry.
    let index = 0;
    let r = INNER_R;
    while (index < group.chips.length) {
      const arc = r * (group.a1 - group.a0);
      const capacity = Math.max(1, Math.floor((arc * 0.86) / SLOT));
      const take = Math.min(capacity, group.chips.length - index);
      const spread = ((take - 1) * SLOT) / r;
      for (let i = 0; i < take; i += 1) {
        const angle =
          take === 1 ? group.mid : group.mid - spread / 2 + (spread * i) / (take - 1);
        const chip = group.chips[index + i];
        chip.x = C + r * Math.cos(angle) - chip.width / 2;
        chip.y = C + r * Math.sin(angle) - CHIP_H / 2;
      }
      index += take;
      r += STEP;
    }
    group.pathR = r - STEP + 14;
    group.labelR = r - STEP + 40;
  }
  return groups;
}

function wedgePath(group: Wedge): string {
  const r = group.pathR;
  const x0 = C + r * Math.cos(group.a0);
  const y0 = C + r * Math.sin(group.a0);
  const x1 = C + r * Math.cos(group.a1);
  const y1 = C + r * Math.sin(group.a1);
  return `M ${C} ${C} L ${x0} ${y0} A ${r} ${r} 0 0 1 ${x1} ${y1} Z`;
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

  const wedges = useMemo(
    () => (state.kind === "ok" ? buildWedges(state.data) : []),
    [state],
  );

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
            Every measured group as a pyramid touching one point: the apex is its best
            name, deeper ranks sit further out along the widening wedge. Stocks, defensive
            assets and crypto rank by balanced score; companies rank within sector by{" "}
            {data.sector_coverage.window_sessions}-session return. Hover a name for its
            measured facts; the tables carry the precision.
          </p>
        </div>
        <div class="discover-compact-meta">
          <a class="btn-secondary compact-button" href="/search">Tables</a>
          <time dateTime={data.as_of}>Updated {new Date(data.as_of).toLocaleString()}</time>
        </div>
      </header>

      <div class="map-canvas">
        <svg
          viewBox={`0 0 ${SIZE} ${SIZE}`}
          class="map-svg"
          role="img"
          aria-label="The measured market as pyramids converging on the single best point"
        >
          {wedges.map((group) => (
            <g key={group.key} data-key={group.key} data-kind={group.kind}>
              <path class="map-wedge" d={wedgePath(group)} fill={group.color} />
              <text
                class="map-wedge-label"
                x={C + group.labelR * Math.cos(group.mid)}
                y={C + group.labelR * Math.sin(group.mid)}
                text-anchor="middle"
                fill={group.color}
              >
                {group.label}
              </text>
              {group.kind === "sector" ? (
                <text
                  class="map-wedge-sublabel"
                  x={C + group.labelR * Math.cos(group.mid)}
                  y={C + group.labelR * Math.sin(group.mid) + 15}
                  text-anchor="middle"
                >
                  {SECTOR_PLAIN[group.label] ?? group.sectorName ?? ""}
                </text>
              ) : null}
              {group.chips.map((chip) => (
                <a key={chip.id} class="map-chip" href={chip.href}>
                  <title>{chip.title}</title>
                  <rect
                    class="map-chip-box"
                    x={chip.x}
                    y={chip.y}
                    width={chip.width}
                    height={CHIP_H}
                    rx={7}
                  />
                  {chip.rank > 0 ? (
                    <text
                      class="map-chip-rank"
                      x={chip.x + 8}
                      y={chip.y + CHIP_H / 2}
                      dominant-baseline="central"
                    >
                      {chip.rank}
                    </text>
                  ) : null}
                  <text
                    class="map-chip-sym"
                    x={chip.x + (chip.rank > 0 ? 21 : 10)}
                    y={chip.y + CHIP_H / 2}
                    dominant-baseline="central"
                  >
                    {chip.label}
                  </text>
                </a>
              ))}
            </g>
          ))}
          <circle class="map-center-glow" cx={C} cy={C} r={16} />
          <circle class="map-center-dot" cx={C} cy={C} r={5} />
          {best ? (
            <>
              <a
                class="map-center-symbol"
                href={`/search?q=${encodeURIComponent(best.symbol)}`}
                text-anchor="middle"
              >
                <title>{assetFacts(best)}</title>
                <text x={C} y={C + 38} text-anchor="middle" dominant-baseline="central">
                  {best.symbol}
                </text>
              </a>
              <text class="map-center-caption" x={C} y={C + 58} text-anchor="middle">
                best measured · balanced {best.scores.balanced?.toFixed(0)}
              </text>
            </>
          ) : (
            <text class="map-center-caption" x={C} y={C + 38} text-anchor="middle">
              nothing fully measured yet
            </text>
          )}
        </svg>
      </div>

      <p class="map-foot">
        Companies shown per sector: top {MAX_SECTOR_LEADERS} of each measured sector's
        ranking · {data.sector_coverage.available} of {data.sector_coverage.total} sectors
        measured · classes show up to {CLASS_CHIP_LIMIT} ranked names, the rest on the
        tables.
      </p>
    </div>
  );
}
