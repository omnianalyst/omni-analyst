import { RegimeView } from "../components/RegimeView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Macro regime | Omni Analyst" };
}

export default function RegimePage() {
  return (
    <div>
      <header class="page-head">
        <h1>Macro regime</h1>
        <p class="muted">
          The system&apos;s read on where the economy sits, composed from FRED
          data. This is the top of the deduction chain — every call below it
          inherits this backdrop.
        </p>
      </header>
      <RegimeView />
    </div>
  );
}
