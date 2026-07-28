export type StalenessTier =
  | "fresh"
  | "recent"
  | "aging"
  | "stale"
  | "dead"
  | "unknown";

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

function plural(n: number, unit: string): string {
  return `${n} ${unit}${n === 1 ? "" : "s"} ago`;
}

export function formatAge(
  totalSeconds: number | null | undefined,
): string {
  if (totalSeconds === null || totalSeconds === undefined) return "no data";
  if (Number.isNaN(totalSeconds)) return "no data";
  const s = Math.floor(totalSeconds);
  if (s < 0) return "just now";
  if (s < MINUTE) return s === 0 ? "just now" : plural(s, "second");
  if (s < HOUR) return plural(Math.floor(s / MINUTE), "minute");
  if (s < DAY) return plural(Math.floor(s / HOUR), "hour");
  const d = Math.floor(s / DAY);
  if (d < 30) return plural(d, "day");
  if (d < 365) return plural(Math.floor(d / 30), "month");
  return plural(Math.floor(d / 365), "year");
}

export function stalenessTier(
  totalSeconds: number | null | undefined,
): StalenessTier {
  if (
    totalSeconds === null ||
    totalSeconds === undefined ||
    Number.isNaN(totalSeconds)
  ) {
    return "unknown";
  }
  if (totalSeconds < 0) return "fresh";
  if (totalSeconds < 1 * DAY) return "fresh";
  if (totalSeconds < 7 * DAY) return "recent";
  if (totalSeconds < 30 * DAY) return "aging";
  if (totalSeconds < 365 * DAY) return "stale";
  return "dead";
}
