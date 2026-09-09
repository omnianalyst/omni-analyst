import { FinanceView } from "../components/FinanceView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Finance | Omni Analyst" };
}

export default function FinancePage() {
  return <FinanceView />;
}
