import { SectorView } from "../components/SectorView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Sectors | Omni Analyst" };
}

export default function SectorsPage() {
  return (
    <div>
      <header class="page-head">
        <h1>Sectors</h1>
        <p class="muted">
          Where each sector stands against the others, and whether it suits the
          current phase of the cycle. The middle of the deduction chain: macro
          sets the backdrop, sectors narrow it, individual calls sit below.
        </p>
      </header>
      <SectorView />
    </div>
  );
}
