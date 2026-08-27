import { useEffect, useMemo, useRef, useState } from "preact/hooks";
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
  asset?: AssetMetric;
  company?: {
    name: string;
    sector: string;
    sectorPlain: string;
    returnWindow: number;
    sessions: number;
  };
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
const MAX_SECTOR_LEADERS = 8;
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
      asset,
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
      company: {
        name: leader.name,
        sector: sector.name,
        sectorPlain: SECTOR_PLAIN[sector.symbol] ?? sector.name.split(" ").slice(0, 2).join(" "),
        returnWindow: leader.return_window,
        sessions: data.sector_coverage.window_sessions,
      },
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

const RISK_TIER_WORDS: Record<string, string> = {
  low: "steady (under 10% volatility)",
  medium: "balanced (10-30% volatility)",
  high: "aggressive (30%+ volatility)",
  unrated: "not tiered",
};

const BEHAVIOR_WORDS: Record<string, string> = {
  risk_on: "moves with stocks",
  diversifier: "diversifier",
  counterweight: "counterweight",
  unrated: "not measured",
};

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;
}

// The hover card: the full measured breakdown behind a chip, in HTML over
// the SVG so it inherits the page's type and never scales with the canvas.
function MapPopover({ chip, x, y }: { chip: Chip; x: number; y: number }) {
  if (chip.asset) {
    const a = chip.asset;
    const components: Array<[string, number | null | undefined]> = [
      ["growth", a.scores.durable_growth],
      ["consistency", a.scores.consistency],
      ["stability", a.scores.stability],
      ["downside", a.scores.downside],
      ["diversification", a.scores.diversification],
    ];
    const present = components.filter(([, v]) => typeof v === "number") as Array<[string, number]>;
    const worst = present.length >= 2
      ? present.reduce((min, item) => (item[1] < min[1] ? item : min))[0]
      : null;
    return (
      <div class="map-popover" style={`left:${x}px;top:${y}px`}>
        <header>
          <strong>{a.symbol}</strong>
          <span>{a.name}</span>
        </header>
        <p class="map-pop-sub">
          #{chip.rank} in {classLabel(a.asset_class)} · {RISK_TIER_WORDS[a.risk_tier] ?? a.risk_tier}
        </p>
        <div class="map-pop-score">
          <span>balanced score</span>
          <strong>{a.scores.balanced?.toFixed(0) ?? "—"}</strong>
        </div>
        {present.map(([name, value]) => (
          <div class={`map-pop-bar${name === worst ? " map-pop-bar-worst" : ""}`} key={name}>
            <span>{name === worst ? `${name} · weakest` : name}</span>
            <div class="map-pop-track">
              <div class="map-pop-fill" style={`width:${Math.max(0, Math.min(100, value))}%`} />
            </div>
            <b>{value.toFixed(0)}</b>
          </div>
        ))}
        <dl class="map-pop-facts">
          <div><dt>Median year</dt><dd>{pct(a.median_annual_return)}</dd></div>
          <div><dt>1 year</dt><dd>{pct(a.returns?.["365d"])}</dd></div>
          <div><dt>Volatility</dt><dd>{pct(a.volatility)}</dd></div>
          <div><dt>Max drawdown</dt><dd>{pct(a.max_drawdown)}</dd></div>
          <div><dt>5-year CAGR</dt><dd>{pct(a.cagr_5y)}</dd></div>
          <div><dt>Positive years</dt><dd>{pct(a.positive_year_rate)}</dd></div>
          <div><dt>Sharpe</dt><dd>{a.sharpe?.toFixed(2) ?? "—"}</dd></div>
          <div><dt>History</dt><dd>{a.complete_years} full of {a.history_years.toFixed(1)}y</dd></div>
          <div><dt>Market role</dt><dd>{BEHAVIOR_WORDS[a.market_behavior] ?? a.market_behavior}</dd></div>
          <div><dt>Corr. to SPY</dt><dd>{a.correlation_to_spy?.toFixed(2) ?? "—"}</dd></div>
        </dl>
      </div>
    );
  }
  const c = chip.company!;
  return (
    <div class="map-popover" style={`left:${x}px;top:${y}px`}>
      <header>
        <strong>{chip.label}</strong>
        <span>{c.name}</span>
      </header>
      <p class="map-pop-sub">
        #{chip.rank} in {c.sectorPlain} · {c.sector}
      </p>
      <div class="map-pop-score">
        <span>{c.sessions}-session return</span>
        <strong class={c.returnWindow > 0 ? "value-positive" : "value-negative"}>
          {pct(c.returnWindow)}
        </strong>
      </div>
      <p class="map-pop-note">
        Company rankings use the {c.sessions}-session history currently measurable --
        shorter than the asset-class record, so the two numbers are not comparable.
      </p>
    </div>
  );
}

function classLabel(cls: string): string {
  return cls === "stocks" ? "Stocks & ETFs" : cls === "defensive" ? "Defensive & real assets" : cls === "crypto" ? "Crypto" : cls;
}

export function MapView() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [pop, setPop] = useState<{ chip: Chip; x: number; y: number } | null>(null);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  // Canvas navigation: pan by drag or plain wheel, zoom with ctrl/cmd+wheel
  // or the buttons, always anchored at the cursor. No scrollbars anywhere.
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const viewRef = useRef(view);
  viewRef.current = view;
  const dragRef = useRef<{ startX: number; startY: number; baseX: number; baseY: number; moved: boolean } | null>(null);
  const lastDragRef = useRef(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (canvas === null) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const cx = event.clientX - rect.left;
      const cy = event.clientY - rect.top;
      const { x, y, k } = viewRef.current;
      if (event.ctrlKey || event.metaKey) {
        const next = Math.min(3, Math.max(0.35, k * Math.exp(-event.deltaY * 0.0022)));
        setView({
          k: next,
          x: cx - ((cx - x) * next) / k,
          y: cy - ((cy - y) * next) / k,
        });
      } else {
        setView({ x: x - event.deltaX, y: y - event.deltaY, k });
      }
      setPop(null);
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", onWheel);
  }, []);

  // The fan's center in stage coordinates: the SVG renders at 1500px for a
  // 1000-unit viewBox, so the center sits at 750px. Centering places that
  // point at the pane's center via the transform -- flex centering cannot be
  // trusted here (`safe center` degrades to start on overflow, which opened
  // the map on its top-left quadrant).
  const STAGE_RENDER = 1500;

  const centerView = () => {
    const canvas = canvasRef.current;
    if (canvas === null) return;
    const rect = canvas.getBoundingClientRect();
    setView({
      x: rect.width / 2 - STAGE_RENDER / 2,
      y: rect.height / 2 - STAGE_RENDER / 2,
      k: 1,
    });
  };

  // Center once the canvas exists (it renders after the payload arrives).
  useEffect(() => {
    if (state.kind === "ok") centerView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.kind]);

  const zoomBy = (factor: number) => {
    const canvas = canvasRef.current;
    if (canvas === null) return;
    const rect = canvas.getBoundingClientRect();
    const cx = rect.width / 2;
    const cy = rect.height / 2;
    const { x, y, k } = viewRef.current;
    const next = Math.min(3, Math.max(0.35, k * factor));
    setView({
      k: next,
      x: cx - ((cx - x) * next) / k,
      y: cy - ((cy - y) * next) / k,
    });
  };

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

  const openPop = (chip: Chip, event: MouseEvent) => {
    const canvas = canvasRef.current;
    if (canvas === null) return;
    const rect = canvas.getBoundingClientRect();
    const W = 292;
    const H = chip.asset ? 428 : 240;
    // Clamp on every edge: the card opens beside the cursor when there is
    // room and shifts inside the pane when there is not, but never spills.
    const pad = 8;
    let x = event.clientX - rect.left + 14;
    let y = event.clientY - rect.top + 14;
    const maxX = Math.max(pad, rect.width - W - pad);
    const maxY = Math.max(pad, rect.height - H - pad);
    x = Math.min(maxX, Math.max(pad, x));
    y = Math.min(maxY, Math.max(pad, y));
    setPop({ chip, x, y });
  };

  const onDragStart = (event: MouseEvent) => {
    if (event.button !== 0) return;
    dragRef.current = {
      startX: event.clientX,
      startY: event.clientY,
      baseX: viewRef.current.x,
      baseY: viewRef.current.y,
      moved: false,
    };
  };

  const onDragMove = (event: MouseEvent) => {
    const drag = dragRef.current;
    if (drag === null) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
    drag.moved = true;
    setPop(null);
    setView((v) => ({ ...v, x: drag.baseX + dx, y: drag.baseY + dy }));
  };

  const onDragEnd = () => {
    // Keep the record through the synthetic click that follows mouseup --
    // it is what tells the click handler the gesture was a drag. Cleared
    // after one click or replaced by the next drag.
    const drag = dragRef.current;
    if (drag !== null && drag.moved) lastDragRef.current = true;
    dragRef.current = null;
  };

  // A drag that crossed a chip must not become that chip's click.
  const swallowDragClick = (event: Event) => {
    if (lastDragRef.current) {
      lastDragRef.current = false;
      event.preventDefault();
      event.stopPropagation();
    }
  };

  return (
    <div class="map-view product-page">
      <header class="map-heading">
        <div>
          <h1>Map</h1>
          <p>
            Every measured group as a pyramid touching one point: the apex is its best
            name, deeper ranks sit further out along the widening wedge. Stocks, defensive
            assets and crypto rank by balanced score; companies rank within sector by{" "}
            {data.sector_coverage.window_sessions}-session return. Drag or scroll to move,
            ctrl/cmd+scroll or the buttons to zoom, hover a name for its measured facts.
          </p>
        </div>
        <div class="discover-compact-meta">
          <div class="portfolio-header-actions">
            <a class="btn-secondary compact-button" href="/search">Verdict</a>
            <a class="btn-secondary compact-button" href="/rankings">Rankings</a>
            <a class="btn-secondary compact-button" href="/objective" title="Ask the system a question">Ask</a>
            <a class="btn-secondary compact-button" href="/search?tab=watchlist">Saved</a>
            <a class="btn-secondary compact-button" href="/search?tab=alerts">Alerts</a>
          </div>
          <time dateTime={data.as_of}>Updated {new Date(data.as_of).toLocaleString()}</time>
        </div>
      </header>

      <div
        class="map-canvas"
        ref={canvasRef}
        onMouseDown={onDragStart}
        onMouseMove={onDragMove}
        onMouseUp={onDragEnd}
        onMouseLeave={onDragEnd}
        onClickCapture={swallowDragClick}
      >
        {pop ? <MapPopover chip={pop.chip} x={pop.x} y={pop.y} /> : null}
        <div
          class="map-stage"
          style={`transform:translate(${view.x}px,${view.y}px) scale(${view.k})`}
        >
        <div class="map-canvas-inner">
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
                <a
                  key={chip.id}
                  class="map-chip"
                  href={chip.href}
                  onMouseOver={(event) => openPop(chip, event)}
                  onMouseOut={(event) => {
                    const to = event.relatedTarget as Node | null;
                    if (!(event.currentTarget as Node).contains(to)) setPop(null);
                  }}
                >
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
        </div>
        <div class="map-controls" role="toolbar" aria-label="Map navigation">
          <button type="button" class="map-ctl" onClick={() => zoomBy(1 / 1.25)} aria-label="Zoom out">−</button>
          <span class="map-zoom-readout">{Math.round(view.k * 100)}%</span>
          <button type="button" class="map-ctl" onClick={() => zoomBy(1.25)} aria-label="Zoom in">+</button>
          <button type="button" class="map-ctl map-ctl-reset" onClick={centerView}>Reset</button>
        </div>
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
