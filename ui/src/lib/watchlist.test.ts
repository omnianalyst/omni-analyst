import { describe, expect, it } from "vitest";
import { entryName, entrySymbol, type WatchlistEntry } from "./watchlist";

function entry(over: Partial<WatchlistEntry> = {}): WatchlistEntry {
  return {
    entity_id: "id",
    kind: "issuer",
    symbol: "AAPL",
    name: "Apple Inc.",
    added_at: null,
    ...over,
  };
}

describe("entrySymbol", () => {
  it("returns the symbol when one is present", () => {
    expect(entrySymbol(entry({ symbol: "AAPL" }))).toBe("AAPL");
  });

  it("renders a dash for a null symbol rather than inventing one", () => {
    expect(entrySymbol(entry({ symbol: null }))).toBe("\u2014");
    expect(entrySymbol(entry({ symbol: null }))).not.toBe("UNKNOWN");
    expect(entrySymbol(entry({ symbol: null }))).not.toBe("");
  });
});

describe("entryName", () => {
  it("returns the name when one is present", () => {
    expect(entryName(entry({ name: "Apple Inc." }))).toBe("Apple Inc.");
  });

  it("renders (unnamed) for a null name rather than fabricating a label", () => {
    expect(entryName(entry({ name: null }))).toBe("(unnamed)");
    expect(entryName(entry({ name: null }))).not.toBe("Apple Inc.");
    expect(entryName(entry({ name: null }))).not.toBe("");
  });
});
