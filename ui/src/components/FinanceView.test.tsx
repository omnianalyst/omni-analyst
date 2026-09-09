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
});
