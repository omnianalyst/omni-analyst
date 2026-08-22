// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { hydrate, render } from "preact";
import { act } from "preact/test-utils";
import renderToString from "preact-render-to-string";

const navigate = vi.hoisted(() => vi.fn());

vi.mock("@neutron-build/core/client", () => ({
  navigate,
  useLocation: () => ({ pathname: window.location.pathname }),
  useNavigate: () => navigate,
}));

import { AlertsView } from "./AlertsView";
import { CreateAlertForm } from "./CreateAlertForm";
import { TrackButton } from "./TrackButton";
import { CommandPalette, OPEN_COMMAND_PALETTE } from "./CommandPalette";
import { DiscoverView } from "./DiscoverView";
import { LoginView } from "./LoginView";
import { PortfolioView } from "./PortfolioView";
import { SetupView } from "./SetupView";
import { SystemView } from "./SystemView";
import { WalletAccounts } from "./WalletAccounts";
import { AUTH_TOKEN_KEY, clearAuthToken } from "../lib/auth";
import {
  __resetForTest,
  state as systemState,
  status as systemStatus,
} from "../lib/systemStore";
import Layout from "../routes/_layout";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function root(): HTMLDivElement {
  const node = document.createElement("div");
  document.body.append(node);
  return node;
}

function setInput(input: HTMLInputElement, value: string): void {
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function button(container: ParentNode, label: string): HTMLButtonElement {
  const match = Array.from(container.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === label,
  );
  if (!(match instanceof HTMLButtonElement)) throw new Error(`button not found: ${label}`);
  return match;
}

async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function waitFor(assertion: () => void): Promise<void> {
  let lastError: unknown;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      lastError = error;
      await settle();
    }
  }
  throw lastError;
}

beforeEach(() => {
  document.body.innerHTML = "";
  window.history.replaceState({}, "", "/");
  localStorage.clear();
  localStorage.setItem(AUTH_TOKEN_KEY, "test-token");
  navigate.mockReset();
  __resetForTest();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  __resetForTest();
  document.body.innerHTML = "";
});

describe("rendered keyboard workflows", () => {
  it("uses the updated palette selection when ArrowDown and Enter are immediate", async () => {
    const container = root();
    await act(async () => {
      render(
        <CommandPalette
          commands={[
            { label: "Portfolio", href: "/" },
            { label: "Discover", href: "/search" },
          ]}
        />,
        container,
      );
    });
    act(() => {
      window.dispatchEvent(new Event(OPEN_COMMAND_PALETTE));
    });
    await settle();
    expect(container.querySelector(".palette")).not.toBeNull();

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown" }));
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    });

    expect(navigate).toHaveBeenCalledWith("/search");
    render(null, container);
  });
});

describe("rendered authentication workflows", () => {
  it("surfaces Setup validation before native minlength can suppress submit", async () => {
    window.history.replaceState({}, "", "/setup");
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const container = root();
    render(<SetupView />, container);

    const inputs = container.querySelectorAll("input");
    await act(() => {
      setInput(inputs[0] as HTMLInputElement, "operator@example.com");
      setInput(inputs[1] as HTMLInputElement, "short");
      setInput(inputs[2] as HTMLInputElement, "different");
    });
    await act(() => button(container, "Create account").click());

    expect(container.textContent).toContain("Passwords do not match.");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("submits valid Setup credentials, stores the token, and redirects", async () => {
    window.history.replaceState({}, "", "/setup");
    const fetchMock = vi.fn().mockResolvedValue(json({
      token: "setup-token",
      token_type: "bearer",
      expires_in: 3600,
      user: { id: "user-1", email: "operator@example.com", created_at: null, active: true },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const container = root();
    render(<SetupView />, container);

    const inputs = container.querySelectorAll("input");
    await act(() => {
      setInput(inputs[0] as HTMLInputElement, "operator@example.com");
      setInput(inputs[1] as HTMLInputElement, "long-enough-password");
      setInput(inputs[2] as HTMLInputElement, "long-enough-password");
    });
    await act(() => button(container, "Create account").click());
    await waitFor(() => expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBe("setup-token"));

    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/auth/setup",
      expect.objectContaining({ method: "POST" }),
    );
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      email: "operator@example.com",
      password: "long-enough-password",
    });
    expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBe("setup-token");
    expect(navigate).toHaveBeenCalledWith("/");
  });

  it("reacts to login token storage and navigates into the private app", async () => {
    localStorage.clear();
    window.history.replaceState({}, "", "/login");
    const fetchMock = vi.fn().mockResolvedValue(json({
      token: "login-token",
      token_type: "bearer",
      expires_in: 3600,
    }));
    vi.stubGlobal("fetch", fetchMock);
    const container = root();
    render(<LoginView />, container);

    const inputs = container.querySelectorAll("input");
    await act(() => {
      setInput(inputs[0] as HTMLInputElement, "operator@example.com");
      setInput(inputs[1] as HTMLInputElement, "password123");
    });
    await act(() => button(container, "Sign in").click());
    await waitFor(() => expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBe("login-token"));

    expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBe("login-token");
    expect(navigate).toHaveBeenCalledWith("/");
  });

  it("removes rendered private route content as soon as auth is cleared", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({}, 503)));
    const container = root();
    render(<Layout data={{ pathname: "/" }}><div>private account data</div></Layout>, container);
    await waitFor(() => expect(container.querySelector('button[title="Settings"]')).not.toBeNull());
    expect(container.textContent).toContain("private account data");

    const redirectTimer = vi.spyOn(window, "setTimeout").mockImplementation((() => 0) as typeof window.setTimeout);
    await act(async () => {
      clearAuthToken();
      await Promise.resolve();
    });

    expect(localStorage.getItem(AUTH_TOKEN_KEY)).toBeNull();
    expect(container.textContent).not.toContain("private account data");
    expect(redirectTimer).toHaveBeenCalled();
  });
});

describe("rendered alerts", () => {
  it("pauses, edits, and deletes an alert with explicit feedback", async () => {
    let alert = {
      id: "alert-1",
      user_id: "user-1",
      entity_id: "entity-12345678",
      claim_type: "price_snapshot",
      condition: { kind: "value_above", threshold: 100, field: "value" },
      active: true,
      created_at: "2026-08-14T10:00:00Z",
      last_fired_at: null,
    };
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input);
      if (url.pathname === "/alerts" && (!init?.method || init.method === "GET")) {
        return json({ alerts: [alert] });
      }
      if (url.pathname === "/alerts/alert-1" && init?.method === "PATCH") {
        alert = { ...alert, ...JSON.parse(String(init.body)) };
        return json(alert);
      }
      if (url.pathname === "/alerts/alert-1" && init?.method === "DELETE") {
        return json({ deleted: true });
      }
      return json({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const container = root();
    render(<AlertsView />, container);
    await waitFor(() => expect(container.textContent).toContain("price_snapshot"));

    await act(() => button(container, "Pause").click());
    await settle();
    expect(container.textContent).toContain("Alert paused.");
    expect(container.textContent).toContain("paused");

    await act(() => button(container, "Edit").click());
    const level = container.querySelector('.alert-edit-form input[type="number"]') as HTMLInputElement;
    await act(() => setInput(level, "90"));
    await act(() => button(container, "Save condition").click());
    await settle();
    expect(container.textContent).toContain("value > 90");
    expect(container.textContent).toContain("Alert condition updated.");

    await act(() => button(container, "Delete").click());
    await settle();
    expect(container.textContent).toContain("Alert deleted.");
    expect(container.textContent).toContain("No alerts set.");
    const mutations = fetchMock.mock.calls.filter((call) => call[1]?.method);
    expect(mutations.map((call) => call[1]?.method)).toEqual(["PATCH", "PATCH", "DELETE"]);
  });

  it("hydrates an alert-tab request without a divergent initial tree", async () => {
    window.history.replaceState({}, "", "/search?tab=alerts");
    vi.stubGlobal("fetch", vi.fn(async (input: string) => {
      const path = new URL(input).pathname;
      if (path === "/alerts") return json({ alerts: [] });
      return json({}, 503);
    }));
    const container = root();
    container.innerHTML = renderToString(<DiscoverView />);
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);

    hydrate(<DiscoverView />, container);
    // The tab bar is gone: a ?tab=alerts deep link opens the Alerts overlay
    // after hydration, and the URL is cleaned so a refresh lands on the page.
    await waitFor(() => expect(container.textContent).toContain("No alerts set."));
    await waitFor(() => expect(window.location.search).toBe(""));

    expect(container.textContent).toContain("Saved");
    expect(consoleError).not.toHaveBeenCalled();
  });
});

describe("rendered failure and empty states", () => {
  it("names a failed wallet load and recovers through retry", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ detail: "wallet service offline" }, 503))
      .mockResolvedValueOnce(json({
        accounts: [],
        security: { read_only: true, stores_private_keys: false, stores_seed_phrases: false },
      }));
    vi.stubGlobal("fetch", fetchMock);
    const container = root();
    render(<WalletAccounts />, container);
    await waitFor(() => expect(container.textContent).toContain("Wallet accounts unavailable"));

    expect(container.textContent).toContain("Wallet accounts unavailable");
    expect(container.textContent).not.toContain("No external wallets tracked");
    await act(() => button(container, "Try again").click());
    await waitFor(() => expect(container.textContent).toContain("No external wallets tracked"));
    expect(container.textContent).toContain("No external wallets tracked");
  });

  it("keeps wallets available when the account has no managed portfolio", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: string) => {
      const path = new URL(input).pathname;
      if (path === "/wallets") {
        return json({
          accounts: [],
          security: { read_only: true, stores_private_keys: false, stores_seed_phrases: false },
        });
      }
      if (path === "/holdings") {
        return json({ holdings: [], summary: { positions: 0, priced: 0, total_value: null, total_pnl: null } });
      }
      return json({ detail: "No portfolio for this account" }, 404);
    }));
    const container = root();
    render(<PortfolioView />, container);
    // The personal tracker is the primary surface with no managed book.
    await waitFor(() => expect(container.textContent).toContain("Track what you hold"));

    // Wallets live behind the header button: closed by default, full surface
    // on click. The empty-state text only exists inside the modal.
    const trigger = Array.from(container.querySelectorAll("button"))
      .find((b) => b.textContent?.trim() === "Add wallet")!;
    expect(trigger).toBeDefined();
    expect(container.textContent).not.toContain("No external wallets tracked");
    await act(() => trigger.click());
    await waitFor(() => expect(container.textContent).toContain("No external wallets tracked"));
    expect(container.textContent).toContain("Wallet balances");
  });

  it("renders a named research failure with retry instead of hiding the section", async () => {
    const snapshot = {
      now: "2026-08-14T12:00:00Z",
      loops: [],
      health: { overall: "ok" as const, loops: [] },
      demand: { active: 0, total: 0 },
  claims: { total: 0, last_24h: 0 },
      fill_last_hour: {},
      production_24h: { predictions: 0, findings: 0 },
    };
    systemStatus.value = snapshot;
    systemState.value = "ok";
    vi.stubGlobal("fetch", vi.fn(async (input: string) => {
      const path = new URL(input).pathname;
      if (path === "/system/status") return json(snapshot);
      return json({ detail: "research store offline" }, 503);
    }));
    const container = root();
    render(<SystemView />, container);
    await waitFor(() => expect(container.textContent).toContain("Research record unavailable"));

    expect(container.textContent).toContain("Research record unavailable");
    expect(container.textContent).toContain("research store offline");
    expect(button(container, "Try again")).toBeInstanceOf(HTMLButtonElement);
  });

  it("renders last success and error for scheduled units", async () => {
    const snapshot = {
      now: "2026-08-14T12:00:00Z",
      loops: [],
      health: {
        overall: "failing" as const,
        loops: [
          {
            loop: "nav",
            state: "ok" as const,
            last_status: "success" as const,
            last_success_at: "2026-08-14T07:40:00Z",
            last_failure_at: null,
            consecutive_failures: 0,
            last_error: null,
            last_result: "NAV snapshot 210.12",
            expected_interval_seconds: 86400,
          },
          {
            loop: "shadow_scoring",
            state: "failing" as const,
            last_status: "failure" as const,
            last_success_at: "2026-08-13T18:30:00Z",
            last_failure_at: "2026-08-14T18:30:00Z",
            consecutive_failures: 1,
            last_error: "RuntimeError: price panel unavailable",
            last_result: null,
            expected_interval_seconds: 86400,
          },
        ],
      },
      demand: { active: 0, total: 0 },
  claims: { total: 0, last_24h: 0 },
      fill_last_hour: {},
      production_24h: { predictions: 0, findings: 0 },
    };
    systemStatus.value = snapshot;
    systemState.value = "ok";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({}, 503)));
    const container = root();
    render(<SystemView />, container);

    await waitFor(() => expect(container.textContent).toContain("View technical details"));
    // The technical-details button specifically: the Track record fold above
    // it is also a .disclosure-button now.
    const details = Array.from(container.querySelectorAll<HTMLButtonElement>(".disclosure-button"))
      .find((b) => b.textContent?.includes("technical details"));
    await act(() => details!.click());

    expect(container.textContent).toContain("Last execution result");
    expect(container.textContent).toContain("NAV snapshot 210.12");
    expect(container.textContent).toContain("2026-08-14 07:40:00");
    const scoringRow = Array.from(container.querySelectorAll("tr")).find((row) =>
      row.textContent?.includes("shadow_scoring"),
    );
    expect(scoringRow?.textContent).toContain("RuntimeError: price panel unavailable");
    expect(container.textContent).toContain("Every day");
  });
});

  it("adds a manual holding, shows it valued, and keeps unpriced honest", async () => {
    let holdings: unknown[] = [];
    let nextId = 1;
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const path = url.pathname;
      const method = init?.method ?? "GET";
      if (path === "/holdings" && method === "GET") {
        return json({
          holdings,
          summary: {
            positions: holdings.length,
            priced: holdings.length,
            total_value: holdings.length ? 1000 : null,
            total_pnl: null,
          },
        });
      }
      if (path === "/holdings" && method === "POST") {
        const body = JSON.parse(String(init?.body));
        holdings = [...holdings, { id: `h${nextId++}`, symbol: body.symbol, quantity: Number(body.quantity), cost_basis: null, currency: "USD", note: null, created_at: "2026-08-19T00:00:00Z", updated_at: "2026-08-19T00:00:00Z", last_price: 100, price_as_of: "2026-08-19T00:00:00Z", value: Number(body.quantity) * 100, unrealized_pnl: null, valuation: "priced" }];
        return json(holdings[holdings.length - 1], 201);
      }
      if (path === "/trading/portfolio") {
        return json({ detail: "No portfolio for this account" }, 404);
      }
      return json({ detail: "not found" }, 404);
    }));
    const container = root();
    render(<PortfolioView />, container);

    await waitFor(() => expect(container.textContent).toContain("Track what you hold"));
    await waitFor(() =>
      expect(Array.from(container.querySelectorAll("button"))
        .find((b) => b.textContent?.trim() === "Add position")).toBeDefined());

    // The add form lives behind the Add position button now: closed by
    // default, form present on click.
    expect(container.querySelector("input[placeholder='BTC, ETH, SPY…']")).toBeNull();
    const addTrigger = Array.from(container.querySelectorAll("button"))
      .find((b) => b.textContent?.trim() === "Add position")!;
    await act(() => addTrigger.click());
    await waitFor(() =>
      expect(container.querySelector<HTMLInputElement>("input[placeholder='BTC, ETH, SPY…']")).not.toBeNull());

    const symbolInput = container.querySelector<HTMLInputElement>("input[placeholder='BTC, ETH, SPY…']");
    const quantityInput = container.querySelector<HTMLInputElement>("input[placeholder='0.5']");
    expect(symbolInput).not.toBeNull();
    expect(quantityInput).not.toBeNull();
    await act(() => {
      setInput(symbolInput!, "BTC");
      setInput(quantityInput!, "10");
    });
    await act(() => {
      container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    await waitFor(() => expect(container.textContent).toContain("BTC"));
    expect(container.textContent).toContain("1,000");
  });

describe("the alert form gives price context for the chosen subject", () => {
  it("shows the latest covered price beside the threshold field", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname;
      const method = init?.method ?? "GET";
      if (path === "/entities" && method === "GET") {
        return json({ query: "AAPL", entities: [
          { id: "e-1", kind: "company", symbol: "AAPL", name: "Apple Inc." },
        ]});
      }
      if (path === "/entities/e-1/profile") {
        return json({
          entity: { id: "e-1", kind: "company", symbol: "AAPL", name: "Apple Inc." },
          price: { latest: 207.14, as_of: "2026-08-20T00:00:00Z", returns: {} },
          risk: { risk_tier: "unrated", volatility: null },
          derived: [], fundamentals: [],
        });
      }
      return json({ detail: "not found" }, 404);
    }));
    localStorage.setItem(AUTH_TOKEN_KEY, "token");
    const container = root();
    render(<CreateAlertForm onCreated={() => {}} />, container);

    const query = container.querySelector<HTMLInputElement>("input[placeholder='e.g. AAPL']")!;
    await act(() => setInput(query, "AAPL"));
    await act(() => button(container, "Search").click());
    await waitFor(() =>
      expect(container.querySelector(".entity-list button")).not.toBeNull());
    await act(() => (container.querySelector(".entity-list button") as HTMLButtonElement).click());

    // The threshold context appears once a subject is chosen and priced.
    await waitFor(() =>
      expect(container.textContent).toContain("AAPL last covered at $207.14"));
    expect(container.textContent).toContain("close of 2026-08-20");
    localStorage.removeItem(AUTH_TOKEN_KEY);
  });
});

describe("the entity page's Track button", () => {
  it("creates a watchlist on first track and adds the entity", async () => {
    let watchlists: unknown[] = [];
    let added: string[] = [];
    let removed: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const path = url.pathname;
      const method = init?.method ?? "GET";
      if (path === "/watchlists" && method === "GET") {
        return json({ watchlists });
      }
      if (path === "/watchlists" && method === "POST") {
        const created = { id: "wl-1", name: "Watchlist", created_at: "2026-08-21T00:00:00Z" };
        watchlists = [created];
        return json(created, 201);
      }
      if (path === "/watchlists/wl-1/entries" && method === "POST") {
        added.push(String(JSON.parse(String(init?.body)).entity_id));
        return json({ watchlist_id: "wl-1", entity_id: "e-9", added_at: "2026-08-21T00:00:00Z" }, 201);
      }
      if (path === "/watchlists/wl-1/entries" && method === "GET") {
        return json({ entries: added.map((id) => ({
          entity_id: id, kind: "etf", symbol: "BOTZ", name: "BOTZ", added_at: "2026-08-21T00:00:00Z",
        })) });
      }
      if (path === "/watchlists/wl-1/entries/e-9" && method === "DELETE") {
        removed.push("e-9");
        added = added.filter((id) => id !== "e-9");
        return json({ removed: true });
      }
      return json({ detail: "not found" }, 404);
    }));
    localStorage.setItem(AUTH_TOKEN_KEY, "token");
    const container = root();
    render(<TrackButton entityId="e-9" />, container);

    // The button checks tracked state first; wait for the check to land.
    await waitFor(() => expect(button(container, "Track")).toBeDefined());
    await act(() => button(container, "Track").click());
    await waitFor(() => {
      const b = button(container, "Tracking");
      expect(b).toBeDefined();
      expect(b.disabled).toBe(false);
    });
    expect(added).toEqual(["e-9"]);

    // Un-track: the same button removes and withdraws demand.
    await act(() => button(container, "Tracking").click());
    await waitFor(() => expect(button(container, "Track")).toBeDefined());
    expect(removed).toEqual(["e-9"]);
    localStorage.removeItem(AUTH_TOKEN_KEY);
  });
});
