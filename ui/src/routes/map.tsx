import { MapView } from "../components/MapView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Map | Omni Analyst" };
}

export default function MapPage() {
  return <MapView />;
}
