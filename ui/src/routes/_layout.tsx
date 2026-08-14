import { useEffect, useState } from "preact/hooks";
import { useLocation } from "@neutron-build/core/client";
import "../styles/global.css";
import { CommandPalette, OPEN_COMMAND_PALETTE } from "../components/CommandPalette";
import { HeaderBulletin } from "../components/HeaderBulletin";
import { StatusRail } from "../components/StatusRail";
import type { CommandItem } from "../lib/command";
import {
  AUTH_STATE_EVENT,
  AUTH_TOKEN_KEY,
  clearAuthToken,
  getAuthToken,
} from "../lib/auth";
import { fetchSetupStatus } from "../lib/auth";

export const config = { hydrate: true };

export function head() {
  return { title: "Omni Analyst" };
}

// The pathname, from the framework's own request-aware mechanism.
//
// useLocation() reads RouterContext, and Neutron mounts that provider only in
// the client hydrate path -- there is none in the server renderer, so on the
// server the hook silently returns the createContext default "/" and every
// page believes it is the home route. That made the sign-in page render the
// full nine-link app nav server-side, which then vanished on hydration.
// Reported in Neutron/docs/ADOPTION_FINDINGS.md; a layout loader is correct on
// both sides in the meantime.
export async function loader({ request }: { request: Request }) {
  return { pathname: new URL(request.url).pathname };
}

interface LayoutData {
  pathname?: string;
}

function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(p + "/"));
}

// Grouped by what the operator came to do: read what the system says, look
// something up, or check on the machine itself. /console is absent on purpose --
// it was a third rendering of the feed already on "/" and /briefing, and the
// route now redirects.
const NAV = [
  { href: "/", label: "Portfolio" },
  { href: "/search", label: "Discover" },
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

export default function Layout({
  data,
  children,
}: {
  data?: LayoutData;
  children?: preact.ComponentChildren;
}) {
  // Client navigation keeps RouterContext current, so the hook wins once
  // hydrated; the loader value is what makes the first server render correct.
  const { pathname: routerPath } = useLocation();
  const pathname =
    typeof window === "undefined" ? (data?.pathname ?? routerPath) : routerPath;
  const isPublic = isPublicPath(pathname);

  // Auth state is client-only: localStorage is unavailable during SSR, so the
  // link renders as "Sign in" on the server and may flip to "Sign out" after
  // hydration reads the stored token. No router context is read here, so SSR
  // is not at risk.
  const [signedIn, setSignedIn] = useState(false);
  // The guard withholds protected content from a signed-out visitor. It must
  // not withhold it from the server renderer: SSR has no localStorage, so a
  // guard defaulting to false there rendered <main> empty on every route and
  // the whole app arrived as a blank frame that popped in after hydration.
  // Nothing audience-scoped is in the server output -- every panel fetches its
  // own data with a token after mount -- so rendering the shell leaks nothing.
  // On the client the initial state reads storage synchronously, so a
  // signed-out visitor still never paints a protected page.
  const [allowed, setAllowed] = useState(() => {
    if (typeof window === "undefined") return true;
    return isPublicPath(window.location.pathname) || getAuthToken() !== null;
  });

  useEffect(() => {
    const syncAuth = () => {
      const nextSignedIn = getAuthToken() !== null;
      setSignedIn(nextSignedIn);
      if (nextSignedIn) {
        setAllowed(true);
        return;
      }
      if (!isPublicPath(window.location.pathname)) {
        setAllowed(false);
        window.setTimeout(() => window.location.replace("/login"), 0);
      }
    };
    const syncStoredAuth = (event: StorageEvent) => {
      if (event.key === AUTH_TOKEN_KEY) syncAuth();
    };
    window.addEventListener(AUTH_STATE_EVENT, syncAuth);
    window.addEventListener("storage", syncStoredAuth);

    setSignedIn(getAuthToken() !== null);
    const path = window.location.pathname;
    const tokenPresent = getAuthToken() !== null;
    if (isPublicPath(path)) {
      setAllowed(true);
    } else if (tokenPresent) {
      setAllowed(true);
    } else {
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
    }
    return () => {
      window.removeEventListener(AUTH_STATE_EVENT, syncAuth);
      window.removeEventListener("storage", syncStoredAuth);
    };
  }, []);

  const [menuOpen, setMenuOpen] = useState(false);

  function signOut() {
    setAllowed(false);
    clearAuthToken();
  }

  // Close dropdown on outside click
  useEffect(() => {
    if (!menuOpen) return;
    const close = () => setMenuOpen(false);
    const timer = setTimeout(() => {
      document.addEventListener("click", close);
    }, 0);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("click", close);
    };
  }, [menuOpen]);

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
        <div class="topbar-left">
          <a href="/" class="brand">Omni Analyst</a>
          <nav class="topnav">
            {NAV.map((item) => (
              <a key={item.href} href={item.href} class={`topnav-link ${pathname === item.href ? "topnav-link-active" : ""}`}>
                {item.label}
              </a>
            ))}
          </nav>
        </div>
        <div class="topbar-center">
          {signedIn ? (
            <button
              type="button"
              class="palette-trigger"
              title="Search (Cmd or Ctrl+K)"
              onClick={() =>
                window.dispatchEvent(new CustomEvent(OPEN_COMMAND_PALETTE))
              }
            >
              <span>Search</span>
              <kbd class="palette-trigger-kbd">Cmd K</kbd>
            </button>
          ) : null}
        </div>
        <div class="topbar-right">
          {signedIn ? <StatusRail /> : null}
          {signedIn ? <HeaderBulletin /> : null}
          {signedIn ? (
            <div class="gear-menu" onClick={(e) => e.stopPropagation()}>
              <button
                type="button"
                class="gear-icon"
                title="Settings"
                onClick={() => setMenuOpen(!menuOpen)}
              >
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 010 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.241.437-.613.43-.991a6.932 6.932 0 010-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.087.22-.128.332-.183.582-.495.644-.869l.214-1.28Z" />
                  <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0Z" />
                </svg>
              </button>
              {menuOpen ? (
                <div class="gear-dropdown">
                  <a href="/settings" class="gear-dropdown-item" onClick={() => setMenuOpen(false)}>
                    Settings
                  </a>
                  <button type="button" class="gear-dropdown-item" onClick={() => { signOut(); setMenuOpen(false); }}>
                    Sign out
                  </button>
                </div>
              ) : null}
            </div>
          ) : (
            <a href="/login" class="topnav-auth">Sign in</a>
          )}
        </div>
      </header>
      <main class="content">{allowed ? children : null}</main>
      {signedIn ? <CommandPalette commands={COMMANDS} /> : null}
    </div>
  );
}
