// @vitest-environment jsdom

import { cleanup, fireEvent, render } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { VenueCard } from "./VenueCard";
import type { VenueEntry, VenueLiveStatus } from "../lib/settings";

afterEach(cleanup);

function entry(overrides: Partial<VenueEntry> = {}): VenueEntry {
  return {
    key: "questrade",
    label: "Questrade (Read-Only)",
    type: "equity",
    connectable: true,
    requires_process: false,
    description: "Read-only account data.",
    fields: [
      { name: "refresh_token", label: "Refresh Token", type: "password", required: true },
      {
        name: "mode",
        label: "Mode",
        type: "select",
        options: ["paper", "live"],
        required: true,
      },
    ],
    configured: false,
    enabled: false,
    configuration_source: "unavailable",
    ...overrides,
  };
}

function status(overrides: Partial<VenueLiveStatus> = {}): VenueLiveStatus {
  return {
    key: "questrade",
    status: "not_configured",
    checked_at: "2026-08-14T12:00:00Z",
    error: null,
    positions: [],
    balances: [],
    ...overrides,
  };
}

describe("VenueCard", () => {
  it("renders a real select and blocks submission until required fields are complete", () => {
    const view = render(h(VenueCard, { entry: entry(), status: status(), onChanged: vi.fn() }));

    fireEvent.click(view.getByRole("button", { name: "Add credentials" }));

    const select = view.getByRole("combobox", { name: /Mode/ }) as HTMLSelectElement;
    const token = view.getByLabelText(/Refresh Token/) as HTMLInputElement;
    const submit = view.getByRole("button", { name: "Store encrypted" }) as HTMLButtonElement;
    expect(select.tagName).toBe("SELECT");
    expect(Array.from(select.options).map((option) => option.value)).toEqual(["", "paper", "live"]);
    expect(submit.disabled).toBe(true);

    fireEvent.input(token, { target: { value: "token" } });
    fireEvent.change(select, { target: { value: "paper" } });

    expect(submit.disabled).toBe(false);
  });

  it("shows the checked failure instead of calling enabled state connected", () => {
    const view = render(h(VenueCard, {
      entry: entry({ configured: true, enabled: true, configuration_source: "encrypted" }),
      status: status({ status: "error", error: "Questrade timed out" }),
      onChanged: vi.fn(),
    }));

    expect(view.getByText("Connection failed")).toBeTruthy();
    expect(view.getByText("Questrade timed out")).toBeTruthy();
    expect(view.getAllByText("Enabled")).toHaveLength(2);
    expect(view.queryByText("Connected")).toBeNull();
    expect(view.getByText(/Checked/)).toBeTruthy();
  });

  it("renders Hyperliquid as scheduler-managed without API controls", () => {
    const view = render(h(VenueCard, {
      entry: entry({
        key: "hyperliquid",
        label: "Hyperliquid (Crypto)",
        connectable: false,
        fields: [],
        configuration_source: "deployment",
      }),
      status: status({ key: "hyperliquid", status: "scheduler_only" }),
      onChanged: vi.fn(),
    }));

    expect(view.getByText("Scheduler-managed")).toBeTruthy();
    expect(view.queryByRole("checkbox")).toBeNull();
    expect(view.queryByRole("button")).toBeNull();
  });
});
