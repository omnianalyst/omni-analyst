import { EntityView } from "../../components/EntityView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Entity | Omni Analyst" };
}

export default function EntityPage() {
  return <EntityView />;
}
