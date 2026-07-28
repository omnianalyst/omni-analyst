import { BriefingView } from "../components/BriefingView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Briefing | Omni Analyst" };
}

export default function BriefingPage() {
  return <BriefingView />;
}
