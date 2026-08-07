import { useEffect } from "preact/hooks";

export const config = { mode: "app", hydrate: true };

export function head() {
  return { title: "Omni Analyst — today's read" };
}

// /console was a third view of the same feed shown on "/" and /briefing. It is
// gone from the nav; the path stays so an existing bookmark or open tab lands
// somewhere real instead of a 404.
export default function ConsoleRedirect() {
  useEffect(() => {
    window.location.replace("/");
  }, []);
  return (
    <p class="empty">
      This page has moved to <a href="/">today&apos;s read</a>.
    </p>
  );
}
