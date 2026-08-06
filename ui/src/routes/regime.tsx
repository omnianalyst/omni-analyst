import { RegimeView } from "../components/RegimeView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Macro Regime | Omni Analyst" };
}

export default function RegimePage() {
  return (
    <div>
      <h1 class="page-title">Macro Regime</h1>
      <RegimeView />
    </div>
  );
}
