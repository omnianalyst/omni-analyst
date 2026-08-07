import { ObjectiveView } from "../components/ObjectiveView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Ask | Omni Analyst" };
}

export default function ObjectivePage() {
  return <ObjectiveView />;
}
