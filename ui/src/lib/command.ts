export interface CommandItem {
  label: string;
  href: string;
  // Optional affordance hint shown at the row's right edge (e.g. the digit
  // hotkey "1".."9" for the first nine routes).
  hint?: string;
}

// Substring match across label and href, case-insensitive. An empty query
// returns the full list in original order so the palette can show every
// destination on open without special-casing the empty string at the call site.
export function filterRoutes(
  items: CommandItem[],
  query: string,
): CommandItem[] {
  const q = query.trim().toLowerCase();
  if (!q) return items;
  return items.filter(
    (i) =>
      i.label.toLowerCase().includes(q) ||
      i.href.toLowerCase().includes(q),
  );
}
