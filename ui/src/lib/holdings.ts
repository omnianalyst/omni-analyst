import { authedGetJson, authedSendJson } from "./auth";

export interface ManualHolding {
  id: string;
  symbol: string;
  quantity: number;
  cost_basis: number | null;
  currency: string;
  note: string | null;
  created_at: string;
  updated_at: string;
  last_price: number | null;
  price_as_of: string | null;
  value: number | null;
  unrealized_pnl: number | null;
  valuation: "priced" | "unpriced";
}

export interface HoldingsSummary {
  positions: number;
  priced: number;
  total_value: number | null;
  total_pnl: number | null;
}

export interface HoldingsRecord {
  holdings: ManualHolding[];
  summary: HoldingsSummary;
}

export const getHoldings = (): Promise<HoldingsRecord> =>
  authedGetJson<HoldingsRecord>("/holdings");

export const addHolding = (input: {
  symbol: string;
  quantity: string;
  cost_basis?: string;
  note?: string;
}): Promise<ManualHolding> => authedSendJson<ManualHolding>("POST", "/holdings", input);

export const editHolding = (
  id: string,
  input: { quantity?: string; cost_basis?: string; note?: string },
): Promise<ManualHolding> => authedSendJson<ManualHolding>("PATCH", `/holdings/${id}`, input);

export const removeHolding = (id: string): Promise<void> =>
  authedSendJson<void>("DELETE", `/holdings/${id}`);

/** What the summary line says, without claiming more than was measured. */
export function describeHoldings(summary: HoldingsSummary): string {
  if (summary.positions === 0) {
    return "No positions tracked yet. Add what you hold to see it valued here.";
  }
  if (summary.priced === summary.positions) {
    return `${summary.positions} ${summary.positions === 1 ? "position" : "positions"} valued.`;
  }
  return `${summary.priced} of ${summary.positions} positions valued. A total is shown only when every position has a price.`;
}
