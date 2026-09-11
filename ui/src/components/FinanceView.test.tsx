// @vitest-environment jsdom

import { cleanup, fireEvent, render, waitFor } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FinanceView } from "./FinanceView";

afterEach(cleanup);

type Route = { url: URL; init?: RequestInit };

function mockApi(routes: Record<string, unknown>, calls: Route[] = []) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    calls.push({ url, init });
    const path = url.pathname;
    if (path in routes) {
      return new Response(JSON.stringify(routes[path]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const ACCOUNTS = {
  accounts: [
    {
      id: "acc-1",
      name: "Checking",
      type: "checking",
      currency: "USD",
      offbudget: false,
      closed: false,
      ledger_balance: 123456,
      balance_current: null,
      balance_as_of: null,
    },
    {
      id: "acc-2",
      name: "Euro Savings",
      type: "savings",
      currency: "EUR",
      offbudget: false,
      closed: false,
      ledger_balance: 9900,
      balance_current: null,
      balance_as_of: null,
    },
  ],
};

const CATEGORIES = {
  categories: [
    { id: "cat-1", name: "Groceries", is_income: false },
    { id: "cat-2", name: "Income", is_income: true },
  ],
};

const BUDGET = {
  month: "2026-09-01",
  base_currency: "USD",
  income: 200000,
  budgeted: 10000,
  available_to_budget: 190000,
  categories: [
    {
      id: "cat-1",
      name: "Groceries",
      is_income: false,
      budgeted: 10000,
      activity: -4210,
      available: 5790,
      rollover: 0,
      rollover_mode: "rollover",
      goal: null,
    },
  ],
};

const TRANSACTIONS = {
  transactions: [
    {
      id: "tx-1",
      date: "2026-09-05",
      amount: -4210,
      notes: null,
      cleared: true,
      reconciled: false,
      payee: "Kroger",
      category: "Groceries",
      account: "Checking",
      transfer: false,
      currency: "USD",
    },
  ],
};

describe("FinanceView", () => {
  it("loads accounts and categories on mount and renders the tab strip", async () => {
    mockApi({
      "/finance/accounts": ACCOUNTS,
      "/finance/categories": CATEGORIES,
    });
    const view = render(h(FinanceView, {}));

    await waitFor(() => {
      expect(view.getByText("overview")).toBeTruthy();
    });
    expect(view.getByText("transactions")).toBeTruthy();
    expect(view.getByText("bank")).toBeTruthy();
    expect(view.getByText("reports")).toBeTruthy();
  });

  it("shows per-account currency on balances instead of assuming USD", async () => {
    mockApi({
      "/finance/accounts": ACCOUNTS,
      "/finance/categories": CATEGORIES,
      "/finance/budget": BUDGET,
    });
    const view = render(h(FinanceView, {}));

    await waitFor(() => {
      expect(view.getByText(/Euro Savings/)).toBeTruthy();
    });
    expect(view.container.textContent).toContain("€99.00");
    expect(view.container.textContent).toContain("$1,234.56");
  });

  it("switches to transactions, fetches with the month, and renders rows", async () => {
    const calls: Route[] = [];
    mockApi(
      {
        "/finance/accounts": ACCOUNTS,
        "/finance/categories": CATEGORIES,
        "/finance/transactions": TRANSACTIONS,
      },
      calls
    );
    const view = render(h(FinanceView, {}));

    await waitFor(() => view.getByText("transactions"));
    fireEvent.click(view.getByText("transactions"));

    await waitFor(() => {
      expect(view.getByText("Kroger")).toBeTruthy();
    });
    expect(view.container.textContent).toMatch(/-?[€$]42\.10/);
    const txCall = calls.find((c) => c.url.pathname === "/finance/transactions");
    expect(txCall).toBeTruthy();
    expect(txCall!.url.searchParams.get("month")).toMatch(/^\d{4}-\d{2}$/);
  });

  it("starts on the bank tab when arriving from a bank redirect", async () => {
    window.history.replaceState(null, "", "/finance?link=some-id");
    mockApi({
      "/finance/accounts": ACCOUNTS,
      "/finance/categories": CATEGORIES,
      "/finance/bank/credentials": {
        providers: [
          { key: "gocardless", label: "GoCardless", fields: ["secret_id"], configured: false },
          { key: "simplefin", label: "SimpleFIN", fields: ["access_url"], configured: false },
        ],
      },
      "/finance/bank/accounts": { accounts: [] },
      "/finance/bank/link/some-id": { status: "SA", accounts: [] },
    });
    const view = render(h(FinanceView, {}));

    await waitFor(() => {
      expect(view.getByText("Link SimpleFIN")).toBeTruthy();
    });
    window.history.replaceState(null, "", "/finance");
  });

  it("the import tab's account dropdown defaults to the first open account, not blank", async () => {
    mockApi({
      "/finance/accounts": ACCOUNTS,
      "/finance/categories": CATEGORIES,
    });
    const view = render(h(FinanceView, {}));

    await waitFor(() => view.getByText("import"));
    fireEvent.click(view.getByText("import"));

    await waitFor(() => {
      const selects = Array.from(
        view.container.querySelectorAll("select")
      ) as HTMLSelectElement[];
      const accountSelect = selects.find((s) =>
        Array.from(s.options).some((o) => o.textContent === "Checking")
      );
      expect(accountSelect).toBeTruthy();
      expect(accountSelect!.value).toBe("acc-1");
    });
  });

  it("Import and Preview stay disabled until an account exists and CSV text is pasted", async () => {
    mockApi({
      "/finance/accounts": ACCOUNTS,
      "/finance/categories": CATEGORIES,
    });
    const view = render(h(FinanceView, {}));

    await waitFor(() => view.getByText("import"));
    fireEvent.click(view.getByText("import"));

    await waitFor(() => {
      const preview = view.getByRole("button", { name: "Preview" }) as HTMLButtonElement;
      expect(preview.disabled).toBe(true);
    });
    const importBtn = view.getByRole("button", { name: "Import" }) as HTMLButtonElement;
    expect(importBtn.disabled).toBe(true);
  });
});

describe("FinanceView month scoping (U16/U19)", () => {
  it("shows the second month's rows when an earlier month's response lands last", async () => {
    const now = new Date();
    const pad = (n: number) => String(n + 1).padStart(2, "0");
    const firstMonth = `${now.getFullYear()}-${pad(now.getMonth())}`;
    const next = new Date(now.getFullYear(), now.getMonth() + 1, 1);
    const secondMonth = `${next.getFullYear()}-${pad(next.getMonth())}`;
    const rowFor = (label: string) => ({
      month: `${label}-01`, base_currency: "USD", income: 1, budgeted: 1,
      available_to_budget: 0,
      categories: [{ id: "cat-1", name: label, is_income: false, budgeted: 1, activity: 0, available: 1, rollover: 0, rollover_mode: "rollover", goal: null }],
    });
    const budgetByMonth: Record<string, unknown> = {
      [firstMonth]: rowFor("SEP-ROW"),
      [secondMonth]: rowFor("OCT-ROW"),
    };
    const pendingFirst: Array<() => void> = [];
    let budgetCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/finance/budget") {
        const month = url.searchParams.get("month") ?? "";
        budgetCalls += 1;
        if (budgetCalls === 1) {
          // The first month's response is held until after the second
          // month's has landed -- the reversed-order race.
          await new Promise<void>((resolve) => { pendingFirst.push(resolve); });
        }
        return new Response(JSON.stringify(budgetByMonth[month] ?? budgetByMonth[secondMonth]), { status: 200 });
      }
      if (url.pathname === "/finance/accounts") return new Response(JSON.stringify(ACCOUNTS), { status: 200 });
      if (url.pathname === "/finance/categories") return new Response(JSON.stringify(CATEGORIES), { status: 200 });
      return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { findByText, queryByText, getByText } = render(h(FinanceView, {}));
    await waitFor(() => getByText("budget"));
    fireEvent.click(getByText("budget"));
    // The first month's budget request is in flight (held) when the month
    // switches; the second month's response lands first.
    await waitFor(() => { expect(pendingFirst).toHaveLength(1); });

    const monthInput = document.querySelector('input[type="month"]') as HTMLInputElement;
    monthInput.value = secondMonth;
    fireEvent.input(monthInput);

    // The second month's fetch resolves while the first month's is still held.
    expect(await findByText("OCT-ROW")).toBeTruthy();
    pendingFirst.pop()?.();
    await new Promise((r) => setTimeout(r, 20));
    expect(queryByText("SEP-ROW")).toBeNull();
  });

  it("a double-clicked manual submit posts exactly one transaction (U19)", async () => {
    const posts: unknown[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/finance/transactions" && init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)));
        await new Promise((r) => setTimeout(r, 30));
        return new Response("{}", { status: 200 });
      }
      if (url.pathname === "/finance/transactions") {
        return new Response(JSON.stringify(TRANSACTIONS), { status: 200 });
      }
      if (url.pathname === "/finance/accounts") return new Response(JSON.stringify(ACCOUNTS), { status: 200 });
      if (url.pathname === "/finance/categories") return new Response(JSON.stringify(CATEGORIES), { status: 200 });
      return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { findByText, getByText } = render(h(FinanceView, {}));
    await waitFor(() => getByText("transactions"));
    fireEvent.click(getByText("transactions"));
    fireEvent.click(await findByText("Add transaction"));

    const dateInput = document.querySelector('input[type="date"]') as HTMLInputElement;
    fireEvent.input(dateInput, { target: { value: "2026-09-11" } });
    const numberInputs = Array.from(document.querySelectorAll('input[type="number"]')) as HTMLInputElement[];
    fireEvent.input(numberInputs[0], { target: { value: "12.34" } });

    const save = await findByText("Save");
    fireEvent.click(save);
    fireEvent.click(save);
    await waitFor(() => {
      expect(posts).toHaveLength(1);
    });
    expect(posts[0]).toMatchObject({ amount: 1234 });
  });
});
