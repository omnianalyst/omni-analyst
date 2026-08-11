import { DiscoverView } from "../components/DiscoverView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Discover | Omni Analyst" };
}

export default function SearchPage() {
  return <DiscoverView />;
}
