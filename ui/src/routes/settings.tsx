import { SettingsView } from "../components/SettingsView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Settings | Omni Analyst" };
}

export default function SettingsPage() {
  return <SettingsView />;
}
