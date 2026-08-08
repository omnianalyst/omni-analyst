import { TradingView } from "../components/TradingView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Trading | Omni Analyst" };
}

export default function TradingPage() {
  return <TradingView />;
}
