import { useEffect, useState } from "preact/hooks";
import "../styles/global.css";
import { clearAuthToken, getAuthToken } from "../lib/auth";

export const config = { hydrate: true };

export function head() {
  return { title: "Omni Analyst — coverage" };
}

const NAV = [
  { href: "/", label: "Search" },
  { href: "/objective", label: "Objective" },
  { href: "/regime", label: "Regime" },
  { href: "/sectors", label: "Sectors" },
  { href: "/watchlist", label: "Watchlists" },
  { href: "/alerts", label: "Alerts" },
  { href: "/briefing", label: "Briefing" },
];

export default function Layout({ children }: { children?: preact.ComponentChildren }) {
  // Auth state is client-only: localStorage is unavailable during SSR, so the
  // link renders as "Sign in" on the server and may flip to "Sign out" after
  // hydration reads the stored token. No router context is read here, so SSR
  // is not at risk.
  const [signedIn, setSignedIn] = useState(false);

  useEffect(() => {
    setSignedIn(getAuthToken() !== null);
  }, []);

  function signOut() {
    clearAuthToken();
    setSignedIn(false);
  }

  return (
    <div class="app-shell">
      <header class="topbar">
        <a href="/" class="brand">
          <span class="brand-mark" aria-hidden="true" />
          Omni Analyst
          <span class="brand-sub">coverage</span>
        </a>
        <nav class="topnav">
          {NAV.map((item) => (
            <a key={item.href} href={item.href} class="topnav-link">
              {item.label}
            </a>
          ))}
          {signedIn ? (
            <button
              type="button"
              class="topnav-auth topnav-signout"
              onClick={signOut}
            >
              Sign out
            </button>
          ) : (
            <a href="/login" class="topnav-auth">Sign in</a>
          )}
        </nav>
      </header>
      <main class="content">{children}</main>
    </div>
  );
}
