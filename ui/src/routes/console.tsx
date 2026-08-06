import { ConsoleView } from "../components/ConsoleView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Console | Omni Analyst" };
}

export default function ConsolePage() {
  return <ConsoleView />;
}
