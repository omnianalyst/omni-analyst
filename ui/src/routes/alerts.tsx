import { AlertsView } from "../components/AlertsView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Alerts | Omni Analyst" };
}

export default function AlertsPage() {
  return <AlertsView />;
}
