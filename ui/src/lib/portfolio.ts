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
  assetClass: PositionAssetClass;
  classRefusal: string | null;
  notional: number | null;
}

/**
 * The class of a held symbol, as the backend's governed universe reports it.
 *
 * `null` is a symbol that universe does not list. It is deliberately not a
 * class: the two hardcoded sets this replaces fell through to "stocks" for
 * anything unrecognised, so every unlisted perp in the book was filed as an
 * equity -- a class nobody measured, printed as one somebody did.
 */
export type PositionAssetClass = string | null;

export interface SymbolClassification {
  symbol: string;
  asset: string;
  asset_class: string | null;
  name: string | null;
  refusal: string | null;
}

export interface ClassificationResponse {
  portfolio_id: string;
  classes: string[];
  symbols: SymbolClassification[];
}

export interface CarrySchedule {
  portfolio_id: string;
  as_of: string;
  rebalance_period_days: number;
  window_opens_hour: number;
  window_closes_hour: number;
  in_rebalance_window: boolean;
  refusal_recording_began_at: string | null;
  last_refusal: CarryRefusal | null;
  last_refusal_unavailable: string | null;
  venues: VenueSchedule[];
}

export interface CarryRefusal {
  venue: string;
  attempted_at: string;
  guard: string;
  reason: string;
  funding_window_opens_at: string | null;
  last_cycle_at: string | null;
  last_completed_at: string | null;
  next_due_at: string | null;
}

export type ScheduleState = "never_run" | "no_completed_cycle" | "holding" | "due";

export interface VenueSchedule {
  venue: string;
  state: ScheduleState;
  detail: string;
  last_refusal: CarryRefusal | null;
  last_cycle_at: string | null;
  last_completed_at: string | null;
  funding_window_opens_at: string | null;
  next_rebalance_due_at: string | null;
  days_until_due: number | null;
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

export const getClassification = (): Promise<ClassificationResponse> =>
  authedGetJson<ClassificationResponse>("/trading/classification");

export const getCarrySchedule = (): Promise<CarrySchedule> =>
  authedGetJson<CarrySchedule>("/trading/schedule");

function assetFromSymbol(symbol: string): string {
  return symbol.split("/")[0]?.split(":")[0] || symbol;
}

/**
 * Index the backend's answer by the symbol as stored.
 *
 * Keyed on the venue symbol rather than a parsed base asset so that no second
 * parser in the browser has to agree with the one on the server -- the two
 * drifting is how `ETH/USDC:USDC` and `ETH/USDC` end up in different classes.
 */
export function classificationIndex(
  response: ClassificationResponse | null,
): Map<string, SymbolClassification> {
  const index = new Map<string, SymbolClassification>();
  for (const entry of response?.symbols ?? []) index.set(entry.symbol, entry);
  return index;
}

export function groupPositions(
  positions: Position[],
  classification: Map<string, SymbolClassification> = new Map(),
): PositionGroup[] {
  const groups = new Map<string, PositionGroup>();
  for (const position of positions) {
    const asset = assetFromSymbol(position.symbol);
    const key = `${position.venue}:${asset}`;
    const classified = classification.get(position.symbol);
    const group = groups.get(key) ?? {
      asset,
      venue: position.venue,
      legs: [],
      hasSpot: false,
      hasPerpetual: false,
      assetClass: null,
      classRefusal: null,
      notional: null,
    };
    // A pair's two legs are the same asset, so the first leg the backend
    // classifies settles it for the group; the refusal is only kept while no
    // leg has produced a class. An unread classification and a symbol the
    // universe does not list are different failures and say so separately --
    // collapsing them is how "we did not ask" starts reading as "there is no
    // answer".
    if (group.assetClass === null) {
      if (classified?.asset_class) {
        group.assetClass = classified.asset_class;
        group.classRefusal = null;
      } else if (classified) {
        group.classRefusal =
          classified.refusal ??
          `the governed universe returned no class for ${position.symbol}`;
      } else {
        group.classRefusal =
          classification.size === 0
            ? "the backend classification has not been read"
            : `no entry in the governed universe classifies ${position.symbol}`;
      }
    }
    group.legs.push(position);
    group.hasSpot ||= position.market_type === "spot";
    group.hasPerpetual ||= position.market_type === "perpetual";
    const notional = position.notional === null ? null : Number(position.notional);
    if (notional !== null && Number.isFinite(notional)) {
      group.notional = (group.notional ?? 0) + Math.abs(notional);
    }
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
