import { describe, expect, it } from "vitest";
import { filterRoutes, type CommandItem } from "./command";

const ITEMS: CommandItem[] = [
  { label: "Search", href: "/search", hint: "1" },
  { label: "Regime", href: "/regime", hint: "3" },
  { label: "Sector Scan", href: "/sectors", hint: "4" },
  { label: "System status", href: "/system" },
];

describe("filterRoutes", () => {
  it("returns every destination for an empty query so the palette lists all on open", () => {
    expect(filterRoutes(ITEMS, "")).toEqual(ITEMS);
    expect(filterRoutes(ITEMS, "   ")).toEqual(ITEMS);
  });

  it("matches by label substring, case-insensitive", () => {
    expect(filterRoutes(ITEMS, "reg").map((i) => i.label)).toEqual(["Regime"]);
    expect(filterRoutes(ITEMS, "SECTOR").map((i) => i.label)).toEqual([
      "Sector Scan",
    ]);
  });

  it("matches by href substring", () => {
    expect(filterRoutes(ITEMS, "/sy").map((i) => i.label)).toEqual([
      "System status",
    ]);
  });

  it("returns nothing when no destination matches, so the UI can say so honestly", () => {
    expect(filterRoutes(ITEMS, "zzz")).toEqual([]);
  });

  it("preserves the original order of matches", () => {
    expect(filterRoutes(ITEMS, "s").map((i) => i.label)).toEqual([
      "Search",
      "Sector Scan",
      "System status",
    ]);
  });
});
