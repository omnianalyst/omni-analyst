import { expect, test, type Page, type Request } from "@playwright/test";

const AUTH_TOKEN_KEY = "omni.auth.token";

interface ApiReply {
  status?: number;
  body?: unknown;
}

type ApiHandler = (path: string, request: Request) => ApiReply | undefined;

async function mockApi(page: Page, handler: ApiHandler): Promise<void> {
  await page.route("**/__api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^\/__api/, "");
    const reply = handler(path, request) ?? {
      status: 503,
      body: { detail: `No browser fixture for ${request.method()} ${path}` },
    };
    await route.fulfill({
      status: reply.status ?? 200,
      contentType: "application/json",
      body: JSON.stringify(reply.body ?? {}),
    });
  });
}

async function waitForClient(page: Page): Promise<void> {
  await page.waitForLoadState("networkidle");
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
}

async function openAuthenticated(page: Page, path: string): Promise<void> {
  await page.goto("/login");
  await page.evaluate(
    ([key, token]) => localStorage.setItem(key, token),
    [AUTH_TOKEN_KEY, "browser-token"],
  );
  await page.goto(path);
  await waitForClient(page);
}

test("first-run setup submits credentials, stores the session, and enters the app", async ({ page }) => {
  let submitted: unknown;
  await mockApi(page, (path, request) => {
    if (path === "/auth/setup" && request.method() === "POST") {
      submitted = request.postDataJSON();
      return {
        body: {
          token: "setup-browser-token",
          token_type: "bearer",
          expires_in: 3600,
          user: {
            id: "operator-1",
            email: "operator@example.com",
            created_at: null,
            active: true,
          },
        },
      };
    }
    return undefined;
  });

  await page.goto("/setup");
  await waitForClient(page);
  await page.getByLabel("Email").fill("operator@example.com");
  await page.getByLabel("Password", { exact: true }).fill("long-enough-password");
  await page.getByLabel("Confirm password").fill("long-enough-password");
  await page.getByRole("button", { name: "Create account" }).click();

  await expect(page).toHaveURL("/");
  expect(submitted).toEqual({
    email: "operator@example.com",
    password: "long-enough-password",
  });
  await expect.poll(() => page.evaluate((key) => localStorage.getItem(key), AUTH_TOKEN_KEY))
    .toBe("setup-browser-token");
});

test("login enters the private shell and header logout clears it", async ({ page }) => {
  let submitted: unknown;
  await mockApi(page, (path, request) => {
    if (path === "/auth/login" && request.method() === "POST") {
      submitted = request.postDataJSON();
      return {
        body: { token: "login-browser-token", token_type: "bearer", expires_in: 3600 },
      };
    }
    return undefined;
  });

  await page.goto("/login");
  await waitForClient(page);
  await page.getByLabel("Email").fill("operator@example.com");
  await page.getByLabel("Password").fill("password123");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL("/");
  expect(submitted).toEqual({ email: "operator@example.com", password: "password123" });
  await page.getByTitle("Settings").click();
  await page.getByRole("button", { name: "Sign out" }).click();

  await expect(page).toHaveURL("/login");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  expect(await page.evaluate((key) => localStorage.getItem(key), AUTH_TOKEN_KEY)).toBeNull();
});

test("a signed-out protected route redirects before private settings render", async ({ page }) => {
  await mockApi(page, (path) => {
    if (path === "/auth/setup-status") return { body: { setup_required: false } };
    return undefined;
  });

  await page.goto("/settings");

  await expect(page).toHaveURL("/login");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toHaveCount(0);
});

test("Settings renders user-managed and scheduler-managed venue truth", async ({ page }) => {
  await mockApi(page, (path) => {
    if (path === "/settings/config") {
      return {
        body: {
          provider_catalog: [
            {
              key: "fred",
              label: "FRED",
              category: "Macro",
              settings_field: "fred_api_key",
              key_required: true,
              wired: true,
              configured: true,
            },
          ],
          venue_catalog: [
            {
              key: "questrade",
              label: "Questrade (Read-Only)",
              type: "equity",
              connectable: true,
              requires_process: false,
              description: "Read-only account data.",
              fields: [],
              configured: true,
              enabled: true,
              configuration_source: "encrypted",
            },
            {
              key: "hyperliquid",
              label: "Hyperliquid (Crypto)",
              type: "crypto",
              connectable: false,
              requires_process: false,
              description: "Scheduler-owned carry venue.",
              fields: [],
              configured: false,
              enabled: false,
              configuration_source: "deployment",
            },
          ],
        },
      };
    }
    if (path === "/settings/venues/status") {
      return {
        body: {
          checked_at: "2026-08-14T12:00:00Z",
          venues: [
            {
              key: "questrade",
              status: "connected",
              checked_at: "2026-08-14T12:00:00Z",
              error: null,
              positions: [],
              balances: [],
            },
            {
              key: "hyperliquid",
              status: "scheduler_only",
              checked_at: "2026-08-14T12:00:00Z",
              error: null,
              positions: [],
              balances: [],
            },
          ],
        },
      };
    }
    return undefined;
  });

  await openAuthenticated(page, "/settings");

  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();
  await expect(page.getByText("Questrade (Read-Only)")).toBeVisible();
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();
  await expect(page.getByText("Scheduler-managed", { exact: true })).toBeVisible();
  await expect(page.getByText("FRED", { exact: true })).toBeVisible();
});

test("wallet API failure stays distinct from an empty wallet list", async ({ page }) => {
  await mockApi(page, (path) => {
    if (path === "/holdings") {
      return { status: 200, body: { holdings: [], summary: { positions: 0, priced: 0, total_value: null, total_pnl: null } } };
    }
    if (path === "/wallets") {
      return { status: 503, body: { detail: "wallet service offline" } };
    }
    return undefined;
  });

  await openAuthenticated(page, "/");

  // Wallets live behind the header button; opening it surfaces the named
  // failure rather than an empty list.
  await page.getByRole("button", { name: "Add wallet" }).click();
  await expect(page.getByText("Wallet accounts unavailable", { exact: true })).toBeVisible();
  await expect(page.getByText("No external wallets tracked")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
});

test("keyboard shortcuts open the palette and navigate without a pointer", async ({ page }) => {
  await mockApi(page, () => undefined);
  await openAuthenticated(page, "/");
  await expect(page.getByTitle("Search (Cmd or Ctrl+K)")).toBeVisible();

  await page.keyboard.press("Control+K");
  await expect(page.getByPlaceholder("Search stocks, crypto, ETFs, or jump to a page...")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByPlaceholder("Search stocks, crypto, ETFs, or jump to a page...")).toHaveCount(0);
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  }));
  await page.keyboard.press("2");

  await expect(page).toHaveURL("/search");
  await expect(page.getByRole("button", { name: "Saved" })).toBeVisible();
});

test("alerts hydration emits no mismatch or uncaught browser error", async ({ page }) => {
  const browserFailures: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error" && /hydrat|server html|did not match/i.test(message.text())) {
      browserFailures.push(`console: ${message.text()}`);
    }
  });
  page.on("pageerror", (error) => browserFailures.push(`pageerror: ${error.message}`));
  await mockApi(page, (path) => {
    if (path === "/alerts") return { body: { alerts: [] } };
    return undefined;
  });
  await page.addInitScript(
    ([key, token]) => localStorage.setItem(key, token),
    [AUTH_TOKEN_KEY, "browser-token"],
  );

  await page.goto("/search?tab=alerts");
  // The tab bar is gone: the deep link opens the Alerts overlay after
  // hydration without a divergent initial tree.
  await expect(page.getByText("No alerts set.", { exact: false })).toBeVisible();

  expect(browserFailures).toEqual([]);
});

test("390px login layout has no horizontal overflow and keeps the form usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeInViewport();
  const widths = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  expect(widths).toEqual({ viewport: 390, document: 390, body: 390 });
});
