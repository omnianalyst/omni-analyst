import { WatchlistView } from "../components/WatchlistView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Watchlist | Omni Analyst" };
}

export default function WatchlistPage() {
  return <WatchlistView />;
}
