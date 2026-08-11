import { BookView } from "../components/BookView";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Book | Omni Analyst" };
}

export default function BookPage() {
  return <BookView />;
}
