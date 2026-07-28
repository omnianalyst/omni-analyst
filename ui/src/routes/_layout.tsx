import "../styles/global.css";

export const config = { hydrate: true };

export function head() {
  return { title: "Omni Analyst — coverage" };
}

export default function Layout({ children }: { children?: preact.ComponentChildren }) {
  return (
    <div class="app-shell">
      <header class="topbar">
        <a href="/" class="brand">
          <span class="brand-mark" aria-hidden="true" />
          Omni Analyst
          <span class="brand-sub">coverage</span>
        </a>
      </header>
      <main class="content">{children}</main>
    </div>
  );
}
