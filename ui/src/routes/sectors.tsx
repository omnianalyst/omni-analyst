import { SectorView } from "../components/SectorView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Sectors | Omni Analyst" };
}

export default function SectorsPage() {
  return (
    <div>
      <h1 class="page-title">Sector Scan</h1>
      <SectorView />
    </div>
  );
}
