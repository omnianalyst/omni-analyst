import { PortfolioView } from "../components/PortfolioView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Portfolio | Omni Analyst" };
}

export default function PortfolioPage() {
  return <PortfolioView />;
}
