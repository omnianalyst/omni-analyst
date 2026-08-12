import { describe, expect, it } from "vitest";
import {
  formatMoney,
  groupPositions,
  navChange,
  portfolioHealth,
  recordedCarry,
  type CarryCycle,
} from "./portfolio";
import type { Position, ReconciliationReport } from "./trading";

const position = (market_type: "spot" | "perpetual", quantity: string): Position => ({
  venue: "hyperliquid",
  symbol: "BTC/USDC",
  market_type,
  quantity,
  average_entry: "100",
  notional: "100",
  is_short: quantity.startsWith("-"),
  as_of: "2026-08-11T00:00:00+00:00",
});

const cycle = (overrides: Partial<CarryCycle> = {}): CarryCycle => ({
  venue: "hyperliquid",
  as_of: "2026-08-11T00:00:00+00:00",
  funding_since: "2026-08-10T00:00:00+00:00",
  funding_settled_through: null,
  halted: false,
  halt_reason: null,
  abstention: null,
  funding_collected: "2.50",
  fees_paid: "0.25",
  modelled_turnover_cost: "0.10",
  pairs_opened: 1,
  pairs_closed: 0,
  pairs_held: 1,
  ...overrides,
});

const reconciliation = (status: string): ReconciliationReport => ({
  as_of: "2026-08-11T00:00:00+00:00",
  venues: [{ venue: "hyperliquid", status, checked_at: null, discrepancies: [] }],
});

describe("portfolio presentation", () => {
  it("groups the two legs of a carry position by venue and asset", () => {
    const groups = groupPositions([position("spot", "1"), position("perpetual", "-1")]);
    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({
      asset: "BTC",
      venue: "hyperliquid",
      hasSpot: true,
      hasPerpetual: true,
    });
    expect(groups[0].legs).toHaveLength(2);
  });

  it("puts a halt ahead of a clean reconciliation", () => {
    expect(
      portfolioHealth(
        [position("spot", "1")],
        cycle({ halted: true, halt_reason: "stale prices" }),
        reconciliation("reconciled"),
      ),
    ).toEqual({ tone: "critical", headline: "Portfolio halted", detail: "stale prices" });
  });

  it("does not call unverified positions healthy", () => {
    expect(portfolioHealth([position("spot", "1")], cycle(), null).tone).toBe("attention");
    expect(portfolioHealth([position("spot", "1")], cycle(), reconciliation("stale")).tone).toBe("attention");
  });

  it("calls a reconciled open book healthy", () => {
    expect(
      portfolioHealth([position("spot", "1")], cycle(), reconciliation("reconciled")).tone,
    ).toBe("healthy");
  });

  it("subtracts recorded fees and modelled turnover from funding", () => {
    expect(recordedCarry([cycle()])).toBeCloseTo(2.15);
    expect(recordedCarry([])).toBeNull();
  });

  it("reports change only when two valid NAV points exist", () => {
    expect(navChange([{ taken_at: "a", nav: "100", cash: "0", gross_exposure: "0", net_exposure: "0" }])).toBeNull();
    expect(navChange([
      { taken_at: "a", nav: "100", cash: "0", gross_exposure: "0", net_exposure: "0" },
      { taken_at: "b", nav: "110", cash: "0", gross_exposure: "0", net_exposure: "0" },
    ])).toBeCloseTo(10);
  });

  it("refuses to print malformed money as zero", () => {
    expect(formatMoney("211.14")).toBe("$211.14");
    expect(formatMoney(null)).toBe("—");
    expect(formatMoney("not-a-number")).toBe("—");
  });
});
