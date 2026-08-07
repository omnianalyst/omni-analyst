import { useEffect, useState } from "preact/hooks";
import { useLocation } from "@neutron-build/core/client";
import "../styles/global.css";
import { CommandPalette, OPEN_COMMAND_PALETTE } from "../components/CommandPalette";
import { StatusRail } from "../components/StatusRail";
import type { CommandItem } from "../lib/command";
import { clearAuthToken, getAuthToken } from "../lib/auth";
import { fetchSetupStatus } from "../lib/auth";

export const config = { hydrate: true };

export function head() {
  return { title: "Omni Analyst — coverage" };
}

const NAV = [
  { href: "/briefing", label: "Briefing" },
  { href: "/console", label: "Console" },
  { href: "/search", label: "Search" },
  { href: "/objective", label: "Objective" },
  { href: "/regime", label: "Regime" },
  { href: "/sectors", label: "Sectors" },
  { href: "/watchlist", label: "Watchlists" },
  { href: "/alerts", label: "Alerts" },
  { href: "/system", label: "System" },
];

// The command palette jumps from the same set of destinations as the topbar,
// with the first nine carrying a digit hotkey hint. Derived, not duplicated, so
// adding a route to NAV is the only change needed.
const COMMANDS: CommandItem[] = NAV.map((item, i) => ({
  href: item.href,
  label: item.label,
  hint: i < 9 ? String(i + 1) : undefined,
}));

// Routes that render for an unauthenticated visitor. Everything else requires
// a token; the guard below redirects signed-out users away before the page can
// fetch audience-scoped data that would 401.
const PUBLIC_PATHS = ["/login", "/setup"];

export default function Layout({ children }: { children?: preact.ComponentChildren }) {
  // useLocation reads router context, so it is correct during SSR too -- the
  // bare/public render below is decided server-side, no header flash on login.
  const { pathname } = useLocation();
  const isPublic = pathname === "/login" || pathname === "/setup";

  // Auth state is client-only: localStorage is unavailable during SSR, so the
  // link renders as "Sign in" on the server and may flip to "Sign out" after
  // hydration reads the stored token. No router context is read here, so SSR
  // is not at risk.
  const [signedIn, setSignedIn] = useState(false);
  // Guard state: content is withheld until the auth check resolves, so an
  // unauthenticated visitor never sees a protected page shell flash before the
  // redirect fires. "allow" gates the children render.
  const [allowed, setAllowed] = useState(false);

  useEffect(() => {
    setSignedIn(getAuthToken() !== null);
    const path = window.location.pathname;
    const isPublicPath = PUBLIC_PATHS.some(
      (p) => path === p || path.startsWith(p + "/"),
    );
    if (isPublicPath) {
      setAllowed(true);
      return;
    }
    if (getAuthToken() !== null) {
      setAllowed(true);
      return;
    }
    // No token on a protected route: send the visitor where they can get one.
    // First-run (zero users) -> /setup; otherwise -> /login. replace() so the
    // guarded page is not retained in history (back button does not re-land
    // on a page that will immediately bounce them again).
    fetchSetupStatus()
      .then((s) => {
        window.location.replace(s.setup_required ? "/setup" : "/login");
      })
      .catch(() => {
        window.location.replace("/login");
      });
  }, []);

  function signOut() {
    clearAuthToken();
    setSignedIn(false);
  }

  // Public pages (sign-in, first-run setup) render bare: no topbar nav, no
  // status rail, no command palette. Those are app chrome for a signed-in
  // operator -- a sign-in page carrying the full 8-link nav reads as broken,
  // and the auth guard below would withhold their content anyway. A centered
  // card on a quiet page is the shape a sign-in flow should have.
  if (isPublic) {
    return (
      <div class="app-shell">
        <main class="content content-centered">{children}</main>
      </div>
    );
  }

  return (
    <div class="app-shell">
      <header class="topbar">
        <a href="/" class="brand">
          Omni Analyst
        </a>
        <nav class="topnav">
          {signedIn ? (
            <button
              type="button"
              class="palette-trigger"
              title="Jump to a destination (Cmd or Ctrl+K)"
              onClick={() =>
                window.dispatchEvent(new CustomEvent(OPEN_COMMAND_PALETTE))
              }
            >
              <span>Jump to</span>
              <kbd class="palette-trigger-kbd">Cmd K</kbd>
            </button>
          ) : null}
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
      {signedIn ? <StatusRail /> : null}
      <main class="content">{allowed ? children : null}</main>
      {signedIn ? <CommandPalette commands={COMMANDS} /> : null}
    </div>
  );
}
