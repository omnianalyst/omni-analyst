import { BriefView } from "../components/BriefView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Omni Analyst — today's read" };
}

export default function HomePage() {
  return <BriefView />;
}
