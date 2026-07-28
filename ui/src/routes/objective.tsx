import { ObjectiveView } from "../components/ObjectiveView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Objective | Omni Analyst" };
}

export default function ObjectivePage() {
  return <ObjectiveView />;
}
