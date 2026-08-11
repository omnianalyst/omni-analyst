import { ScannerView } from "../components/ScannerView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Portfolio | Omni Analyst" };
}

export default function PortfolioPage() {
  return <ScannerView />;
}
