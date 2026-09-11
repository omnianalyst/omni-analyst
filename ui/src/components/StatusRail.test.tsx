// @vitest-environment jsdom

import { cleanup, render } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, describe, expect, it } from "vitest";
import { StatusRail } from "./StatusRail";
import {
  __resetForTest,
  lastOkAt,
  state as systemState,
  status as systemStatus,
} from "../lib/systemStore";

afterEach(() => {
  cleanup();
  __resetForTest();
});

// A snapshot with a fresh scheduled loop, so the healthy label is the one a
// stale-but-present snapshot would otherwise keep showing.
const HEALTHY = {
  now: "2026-01-01T00:00:00Z",
  loops: [{ loop: "fill", age_seconds: 30, never_run: false }],
  health: { overall: null, loops: [] },
  demand: { active: 0, total: 0 },
  claims: { total: 0, last_24h: 0 },
  fill_last_hour: {},
  production_24h: { predictions: 0, findings: 0 },
};

describe("StatusRail", () => {
  it("labels a healthy snapshot unhealthy once polling fails (U14)", async () => {
    systemStatus.value = HEALTHY as never;
    lastOkAt.value = Date.now() - 120_000;
    systemState.value = "error";

    const { findByText } = render(h(StatusRail, {}));
    expect(await findByText("System unavailable")).toBeTruthy();
  });

  it("labels a snapshot stale when the last success is too old (U14)", async () => {
    systemStatus.value = HEALTHY as never;
    lastOkAt.value = Date.now() - 120_000;
    systemState.value = "ok";

    const { findByText } = render(h(StatusRail, {}));
    expect(await findByText("System status stale")).toBeTruthy();
  });

  it("still says healthy for a fresh snapshot in the ok state (U14)", async () => {
    systemStatus.value = HEALTHY as never;
    lastOkAt.value = Date.now();
    systemState.value = "ok";

    const { findByText } = render(h(StatusRail, {}));
    expect(await findByText("System healthy")).toBeTruthy();
  });
});
