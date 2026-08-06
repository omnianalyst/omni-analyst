import { SystemView } from "../components/SystemView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "System status | Omni Analyst" };
}

export default function SystemPage() {
  return <SystemView />;
}
