import { SetupView } from "../components/SetupView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Setup | Omni Analyst" };
}

export default function SetupPage() {
  return <SetupView />;
}
