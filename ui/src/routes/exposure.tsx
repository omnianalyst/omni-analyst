import { ExposureView } from "../components/ExposureView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Exposure | Omni Analyst" };
}

export default function ExposurePage() {
  return <ExposureView />;
}
