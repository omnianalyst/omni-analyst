import { SearchView } from "../components/SearchView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Search | Omni Analyst" };
}

export default function SearchPage() {
  return <SearchView />;
}
