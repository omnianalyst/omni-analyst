import { BookView } from "../components/BookView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Trading book | Omni Analyst" };
}

export default function TradingBookPage() {
  return <BookView />;
}
