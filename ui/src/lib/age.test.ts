import { describe, expect, it } from "vitest";
import { formatAge, stalenessTier } from "./age";

const DAY = 86400;
const HOUR = 3600;
const MIN = 60;

describe("formatAge", () => {
  it("reports no data for missing values", () => {
    expect(formatAge(null)).toBe("no data");
    expect(formatAge(undefined)).toBe("no data");
    expect(formatAge(NaN)).toBe("no data");
  });

  it("handles the seconds bucket and its boundaries", () => {
    expect(formatAge(0)).toBe("just now");
    expect(formatAge(1)).toBe("1 second ago");
    expect(formatAge(30)).toBe("30 seconds ago");
    expect(formatAge(59)).toBe("59 seconds ago");
  });

  it("handles the minutes bucket and its boundaries", () => {
    expect(formatAge(MIN)).toBe("1 minute ago");
    expect(formatAge(MIN + 1)).toBe("1 minute ago");
    expect(formatAge(2 * MIN)).toBe("2 minutes ago");
    expect(formatAge(HOUR - 1)).toBe("59 minutes ago");
  });

  it("handles the hours bucket and its boundaries", () => {
    expect(formatAge(HOUR)).toBe("1 hour ago");
    expect(formatAge(2 * HOUR)).toBe("2 hours ago");
    expect(formatAge(DAY - 1)).toBe("23 hours ago");
  });

  it("handles the days bucket and its boundaries", () => {
    expect(formatAge(DAY)).toBe("1 day ago");
    expect(formatAge(26 * DAY)).toBe("26 days ago");
    expect(formatAge(29 * DAY)).toBe("29 days ago");
  });

  it("handles the months bucket and its boundaries", () => {
    expect(formatAge(30 * DAY)).toBe("1 month ago");
    expect(formatAge(60 * DAY)).toBe("2 months ago");
    expect(formatAge(364 * DAY)).toBe("12 months ago");
  });

  it("handles the years bucket and its boundaries", () => {
    expect(formatAge(365 * DAY)).toBe("1 year ago");
    expect(formatAge(730 * DAY)).toBe("2 years ago");
  });

  it("treats a claim from this morning as hours, never days", () => {
    const thisMorning = 3 * HOUR;
    expect(formatAge(thisMorning)).toBe("3 hours ago");
    // The exact failure the work order names: a few hours must never read as
    // 26 days. Pin both sides so a regression flips this assertion.
    expect(formatAge(thisMorning)).not.toBe("26 days ago");
  });

  it("treats negative age (clock skew) as just now", () => {
    expect(formatAge(-5)).toBe("just now");
  });
});

describe("stalenessTier", () => {
  it("is fresh within a day", () => {
    expect(stalenessTier(0)).toBe("fresh");
    expect(stalenessTier(DAY - 1)).toBe("fresh");
  });

  it("steps through recent, aging, stale, dead at the right edges", () => {
    expect(stalenessTier(DAY)).toBe("recent");
    expect(stalenessTier(6 * DAY)).toBe("recent");
    expect(stalenessTier(7 * DAY)).toBe("aging");
    expect(stalenessTier(29 * DAY)).toBe("aging");
    expect(stalenessTier(30 * DAY)).toBe("stale");
    expect(stalenessTier(364 * DAY)).toBe("stale");
    expect(stalenessTier(365 * DAY)).toBe("dead");
  });

  it("is unknown when age is missing", () => {
    expect(stalenessTier(null)).toBe("unknown");
    expect(stalenessTier(undefined)).toBe("unknown");
    expect(stalenessTier(NaN)).toBe("unknown");
  });
});
