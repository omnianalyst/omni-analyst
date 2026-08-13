import { describe, expect, it } from "vitest";
import {
  classificationIndex,
  formatMoney,
  groupPositions,
  navChange,
  portfolioHealth,
  recordedCarry,
  type CarryCycle,
  type ClassificationResponse,
} from "./portfolio";
import type { Position, ReconciliationReport } from "./trading";

const classification = (
  symbols: ClassificationResponse["symbols"],
): ClassificationResponse => ({
  portfolio_id: "book",
  classes: ["crypto", "defensive", "stocks"],
  symbols,
});

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

  it("takes the asset class from the backend rather than from a symbol list", () => {
    const groups = groupPositions(
      [position("spot", "1")],
      classificationIndex(
        classification([
          {
            symbol: "BTC/USDC",
            asset: "BTC",
            asset_class: "defensive",
            name: "Bitcoin",
            refusal: null,
          },
        ]),
      ),
    );

    // "defensive" is deliberately not the class anyone would guess for BTC.
    // The old hardcoded set had it in CRYPTO_ASSETS, so a test using a
    // plausible class would pass against both implementations and prove
    // nothing about which one produced the answer.
    expect(groups[0].assetClass).toBe("defensive");
    expect(groups[0].classRefusal).toBeNull();
  });

  it("leaves a symbol the universe does not list unclassified, not a stock", () => {
    const groups = groupPositions(
      [position("perpetual", "-1")],
      classificationIndex(
        classification([
          {
            symbol: "BTC/USDC",
            asset: "BTC",
            asset_class: null,
            name: null,
            refusal: "no entry in the governed display universe classifies BTC",
          },
        ]),
      ),
    );

    expect(groups[0].assetClass).toBeNull();
    expect(groups[0].classRefusal).toBe(
      "no entry in the governed display universe classifies BTC",
    );
  });

  it("distinguishes an unread classification from an unlisted symbol", () => {
    const [unread] = groupPositions([position("spot", "1")]);
    const [unlisted] = groupPositions(
      [position("spot", "1")],
      classificationIndex(
        classification([
          {
            symbol: "OTHER/USDC",
            asset: "OTHER",
            asset_class: "crypto",
            name: "Other",
            refusal: null,
          },
        ]),
      ),
    );

    expect(unread.assetClass).toBeNull();
    expect(unread.classRefusal).toContain("has not been read");
    expect(unlisted.assetClass).toBeNull();
    expect(unlisted.classRefusal).toContain("no entry in the governed universe");
    expect(unread.classRefusal).not.toBe(unlisted.classRefusal);
  });

  it("classifies a pair from whichever leg the backend lists", () => {
    const groups = groupPositions(
      [position("spot", "1"), position("perpetual", "-1")],
      classificationIndex(
        classification([
          {
            symbol: "BTC/USDC",
            asset: "BTC",
            asset_class: "crypto",
            name: "Bitcoin",
            refusal: null,
          },
        ]),
      ),
    );

    expect(groups).toHaveLength(1);
    expect(groups[0].assetClass).toBe("crypto");
    expect(groups[0].classRefusal).toBeNull();
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
