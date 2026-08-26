import { DiscoverView } from "../components/DiscoverView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Rankings | Omni Analyst" };
}

export default function RankingsPage() {
  return <DiscoverView body="rankings" />;
}
