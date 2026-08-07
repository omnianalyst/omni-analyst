import { RecordView } from "../components/RecordView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Track record | Omni Analyst" };
}

export default function RecordPage() {
  return <RecordView />;
}
