import { authedGetJson } from "./auth";

export const ABSENT = "—";

export type RiskTier = "low" | "medium" | "high" | "unrated";

export interface RankedCompany {
  symbol: string;
  name: string;
  price: number | null;
  return_30d: number | null;
  return_90d: number | null;
  return_365d: number | null;
  volatility: number | null;
  risk_tier: RiskTier;
  max_drawdown: number | null;
  sharpe: number | null;
  cagr_5y: number | null;
  positive_year_rate: number | null;
  history_years: number;
  sessions: number;
  scores: { balanced: number | null; components_available: number };
}

export interface CompaniesData {
  companies: RankedCompany[];
  leaders: RankedCompany[];
  risk_census: Record<RiskTier, number>;
  coverage: {
    measured: number;
    with_prices: number;
    too_thin: number;
    min_sessions: number;
  };
  standing: {
    verdict: string;
    report: string;
    scope: string;
    risk_tier: string;
    sharpe: string;
  };
  as_of: string;
}

export const getCompanies = (): Promise<CompaniesData> =>
  authedGetJson<CompaniesData>("/scanner/companies");

export function percent(value: number | null | undefined, places = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return ABSENT;
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(places)}%`;
}

export function tone(value: number | null | undefined): "positive" | "negative" | "" {
  if (value === null || value === undefined || !Number.isFinite(value)) return "";
  if (value === 0) return "";
  return value > 0 ? "positive" : "negative";
}

/** Tiers in fixed order, so an empty one renders as a visible zero.
 *
 * An omitted tier reads as a filter the caller applied. That distinction is the
 * whole reason the census exists: the diversified categories cannot reach
 * `high` at all, and saying so is different from saying nothing.
 */
export function tierRows(
  census: Record<string, number> | undefined,
): { tier: RiskTier; count: number }[] {
  const order: RiskTier[] = ["low", "medium", "high", "unrated"];
  return order.map((tier) => ({ tier, count: census?.[tier] ?? 0 }));
}

/** One line on how much of the universe actually got measured. */
export function describeCoverage(coverage: CompaniesData["coverage"]): string {
  const { measured, with_prices, too_thin, min_sessions } = coverage;
  if (with_prices === 0) return "No company price history is stored yet.";
  const parts = [`${measured} of ${with_prices} companies ranked`];
  if (too_thin > 0) {
    parts.push(`${too_thin} held back for fewer than ${min_sessions} sessions`);
  }
  return `${parts.join(", ")}.`;
}
