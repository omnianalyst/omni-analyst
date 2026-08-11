import { ScannerView } from "../components/ScannerView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Scanner | Omni Analyst" };
}

export default function ScannerPage() {
  return <ScannerView />;
}
