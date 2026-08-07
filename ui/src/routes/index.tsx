import { TodayView } from "../components/TodayView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Omni Analyst — today's read" };
}

export default function HomePage() {
  return <TodayView />;
}
