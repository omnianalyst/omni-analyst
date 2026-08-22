import {
  authedGetJson,
  authedSendJson,
} from "./auth";

export interface Watchlist {
  id: string;
  name: string;
  created_at: string | null;
}

export interface WatchlistsResponse {
  watchlists: Watchlist[];
}

export interface CreatedWatchlist {
  id: string;
  name: string;
  created_at: string | null;
}

export interface WatchlistEntry {
  entity_id: string;
  kind: string;
  symbol: string | null;
  name: string | null;
  latest_price?: number | null;
  latest_as_of?: string | null;
  change_30d?: number | null;
  added_at: string | null;
}

export interface EntriesResponse {
  entries: WatchlistEntry[];
}

export interface CreatedEntry {
  watchlist_id: string;
  entity_id: string;
  added_at: string | null;
}

export const listWatchlists = (): Promise<WatchlistsResponse> =>
  authedGetJson<WatchlistsResponse>("/watchlists");

export const createWatchlist = (name: string): Promise<CreatedWatchlist> =>
  authedSendJson<CreatedWatchlist>("POST", "/watchlists", { name });

export const listEntries = (watchlistId: string): Promise<EntriesResponse> =>
  authedGetJson<EntriesResponse>(
    `/watchlists/${encodeURIComponent(watchlistId)}/entries`,
  );

export const addEntity = (
  watchlistId: string,
  entityId: string,
): Promise<CreatedEntry> =>
  authedSendJson<CreatedEntry>(
    "POST",
    `/watchlists/${encodeURIComponent(watchlistId)}/entries`,
    { entity_id: entityId },
  );

export const removeEntity = (
  watchlistId: string,
  entityId: string,
): Promise<{ removed: boolean }> =>
  authedSendJson<{ removed: boolean }>(
    "DELETE",
    `/watchlists/${encodeURIComponent(watchlistId)}/entries/${encodeURIComponent(entityId)}`,
  );

export function entrySymbol(entry: WatchlistEntry): string {
  return entry.symbol ?? "\u2014";
}

export function entryName(entry: WatchlistEntry): string {
  return entry.name ?? "(unnamed)";
}

export const deleteWatchlist = (
  watchlistId: string,
): Promise<{ deleted: boolean }> =>
  authedSendJson<{ deleted: boolean }>(
    "DELETE",
    `/watchlists/${encodeURIComponent(watchlistId)}`,
  );
