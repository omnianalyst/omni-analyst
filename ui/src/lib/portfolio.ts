import { authedGetJson } from "./auth";
import {
  describeReconciliation,
  type Position,
  type ReconciliationReport,
} from "./trading";

export interface CarryCycle {
  venue: string;
  as_of: string;
  funding_since: string;
  funding_settled_through: string | null;
  halted: boolean;
  halt_reason: string | null;
  abstention: string | null;
  funding_collected: string;
  fees_paid: string;
  modelled_turnover_cost: string;
  pairs_opened: number;
  pairs_closed: number;
  pairs_held: number;
}

export interface NavPoint {
  taken_at: string;
  nav: string;
  cash: string;
  gross_exposure: string;
  net_exposure: string;
}

export interface CarryCyclesResponse {
  portfolio_id: string;
  cycles: CarryCycle[];
}

export interface NavHistoryResponse {
  portfolio_id: string;
  points: NavPoint[];
}

export interface PositionGroup {
  asset: string;
  venue: string;
  legs: Position[];
  hasSpot: boolean;
  hasPerpetual: boolean;
}

export type HealthTone = "healthy" | "attention" | "critical" | "quiet";

export interface PortfolioHealth {
  tone: HealthTone;
  headline: string;
  detail: string;
}

export const getCarryCycles = (): Promise<CarryCyclesResponse> =>
  authedGetJson<CarryCyclesResponse>("/trading/cycles");

export const getNavHistory = (): Promise<NavHistoryResponse> =>
  authedGetJson<NavHistoryResponse>("/trading/nav-history");

function assetFromSymbol(symbol: string): string {
  return symbol.split("/")[0]?.split(":")[0] || symbol;
}

export function groupPositions(positions: Position[]): PositionGroup[] {
  const groups = new Map<string, PositionGroup>();
  for (const position of positions) {
    const asset = assetFromSymbol(position.symbol);
    const key = `${position.venue}:${asset}`;
    const group = groups.get(key) ?? {
      asset,
      venue: position.venue,
      legs: [],
      hasSpot: false,
      hasPerpetual: false,
    };
    group.legs.push(position);
    group.hasSpot ||= position.market_type === "spot";
    group.hasPerpetual ||= position.market_type === "perpetual";
    groups.set(key, group);
  }
  return [...groups.values()].sort((a, b) => a.asset.localeCompare(b.asset));
}

export function portfolioHealth(
  positions: Position[],
  latestCycle: CarryCycle | null,
  reconciliation: ReconciliationReport | null,
): PortfolioHealth {
  if (latestCycle?.halted) {
    return {
      tone: "critical",
      headline: "Portfolio halted",
      detail: latestCycle.halt_reason || "The latest cycle stopped before trading.",
    };
  }

  const venues = reconciliation?.venues ?? [];
  const diverged = venues.find(
    (venue) => describeReconciliation(venue.status).tone === "diverged",
  );
  if (diverged) {
    return {
      tone: "critical",
      headline: "Book and venue disagree",
      detail: `${diverged.venue} needs reconciliation before the book can be trusted.`,
    };
  }

  const unresolved = venues.find(
    (venue) => describeReconciliation(venue.status).tone !== "clear",
  );
  if (unresolved) {
    return {
      tone: "attention",
      headline: "Verification needed",
      detail: `${unresolved.venue} is ${describeReconciliation(unresolved.status).label.toLowerCase()}.`,
    };
  }

  if (positions.length === 0) {
    return {
      tone: "quiet",
      headline: "No open positions",
      detail: latestCycle?.abstention || "The portfolio is currently holding cash.",
    };
  }

  if (venues.length === 0) {
    return {
      tone: "attention",
      headline: "Positions not yet verified",
      detail: "The portfolio has positions but no venue reconciliation record.",
    };
  }

  return {
    tone: "healthy",
    headline: "Portfolio is healthy",
    detail: "Open positions matched their venues at the latest check.",
  };
}

export function formatMoney(value: string | null | undefined): string {
  if (value === null || value === undefined || value.trim() === "") return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(parsed);
}

export function recordedCarry(cycles: CarryCycle[]): number | null {
  if (cycles.length === 0) return null;
  let total = 0;
  for (const cycle of cycles) {
    const funding = Number(cycle.funding_collected);
    const fees = Number(cycle.fees_paid);
    const turnover = Number(cycle.modelled_turnover_cost);
    if (![funding, fees, turnover].every(Number.isFinite)) return null;
    total += funding - fees - turnover;
  }
  return total;
}

export function navChange(points: NavPoint[]): number | null {
  if (points.length < 2) return null;
  const first = Number(points[0].nav);
  const last = Number(points[points.length - 1].nav);
  if (!Number.isFinite(first) || !Number.isFinite(last) || first === 0) return null;
  return ((last - first) / first) * 100;
}
