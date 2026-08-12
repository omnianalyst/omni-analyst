import { authedGetJson } from "./auth";

// Absent is not zero. A dash means the store could not support the figure; a
// zero would be a measurement nobody made.
export const ABSENT = "—";

export interface PricePoint {
  date: string;
  close: number;
}

export interface EntityProfile {
  entity: {
    id: string;
    symbol: string;
    name: string;
    kind: string;
    identifiers: Record<string, string>;
  };
  price: {
    latest: number | null;
    as_of: string | null;
    source: string | null;
    returns: { "30d": number | null; "90d": number | null; "365d": number | null };
    series: PricePoint[];
  };
  risk: {
    volatility: number | null;
    risk_tier: "low" | "medium" | "high" | "unrated";
    max_drawdown: number | null;
    sharpe: number | null;
    correlation_to_market: number | null;
    market_behavior: string;
    sessions: number;
    history_days: number | null;
  };
  fundamentals: {
    key: string;
    label: string;
    value: number;
    unit: string | null;
    higher_is_better: boolean | null;
    period_end: string;
    knowable_from: string;
    fiscal_period: string | null;
    fiscal_year: number | null;
    form: string | null;
    source: string;
  }[];
  derived: {
    market_cap: number | null;
    market_cap_as_of: string | null;
    gross_margin: number | null;
    net_margin: number | null;
  };
  coverage: {
    claim_type: string;
    claims: number;
    newest: string | null;
    oldest_event: string | null;
  }[];
  limits: string[];
  as_of: string;
}

export const getProfile = (entityId: string): Promise<EntityProfile> =>
  authedGetJson<EntityProfile>(`/entities/${entityId}/profile`);

/** Compact currency, at the magnitude a reader actually thinks in. */
export function formatMagnitude(value: number | null | undefined, unit?: string | null): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT;
  const prefix = unit === "USD" ? "$" : "";
  const sign = value < 0 ? "-" : "";
  const size = Math.abs(value);
  const scales: [number, string][] = [
    [1e12, "T"],
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "K"],
  ];
  for (const [factor, suffix] of scales) {
    if (size >= factor) return `${sign}${prefix}${(size / factor).toFixed(2)}${suffix}`;
  }
  return `${sign}${prefix}${size.toFixed(2)}`;
}

export function formatPercent(value: number | null | undefined, places = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT;
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(places)}%`;
}

export function formatNumber(value: number | null | undefined, places = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT;
  return value.toFixed(places);
}

/** Sign class for colouring, neutral when the direction is ambiguous or absent. */
export function toneFor(
  value: number | null | undefined,
  higherIsBetter: boolean | null | undefined = true,
): "positive" | "negative" | "neutral" {
  if (value === null || value === undefined || !Number.isFinite(value)) return "neutral";
  if (higherIsBetter === null || higherIsBetter === undefined) return "neutral";
  const good = higherIsBetter ? value > 0 : value < 0;
  if (value === 0) return "neutral";
  return good ? "positive" : "negative";
}

/** A filing shown as "Q2 FY2026 10-Q", skipping whatever the store lacks. */
export function describeFiling(item: EntityProfile["fundamentals"][number]): string {
  const parts = [
    item.fiscal_period ?? null,
    item.fiscal_year ? `FY${item.fiscal_year}` : null,
    item.form ?? null,
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(" ") : `period ending ${item.period_end}`;
}

/** Points for a sparkline path, normalised into a 0..1 box. */
export function sparklinePath(series: PricePoint[], width: number, height: number): string | null {
  if (series.length < 2) return null;
  const values = series.map((point) => point.close);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  // A perfectly flat series has no shape to draw. Dividing by a zero span would
  // put every point at NaN and render an invisible, broken path.
  if (!Number.isFinite(span) || span <= 0) return null;
  return values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width;
      const y = height - ((value - min) / span) * height;
      return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}
