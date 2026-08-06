import { beforeEach, describe, expect, it, vi, afterEach } from "vitest";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, getAuthToken: vi.fn() };
});
vi.mock("./system", () => ({ getSystemStatus: vi.fn() }));

import { getAuthToken } from "./api";
import { getSystemStatus } from "./system";
import {
  __resetForTest,
  errorMessage,
  lastOkAt,
  refresh,
  start,
  state,
  status,
  stop,
} from "./systemStore";

const token = vi.mocked(getAuthToken);
const fetchStatus = vi.mocked(getSystemStatus);

const OK = {
  now: "2026-01-01T00:00:00Z",
  loops: [],
  demand: { active: 0, total: 0 },
  fill_last_hour: {},
  production_24h: { predictions: 0, findings: 0 },
};

beforeEach(() => {
  __resetForTest();
  token.mockReset();
  fetchStatus.mockReset();
  token.mockReturnValue("token");
});

afterEach(() => {
  __resetForTest();
});

describe("refresh", () => {
  it("does nothing without a token, so a post-sign-out poll cannot 401", async () => {
    token.mockReturnValue(null);
    await refresh();
    expect(fetchStatus).not.toHaveBeenCalled();
    expect(state.value).toBe("idle");
  });

  it("stores the snapshot and marks the state ok on success", async () => {
    fetchStatus.mockResolvedValue(OK);
    await refresh();
    expect(status.value).toBe(OK);
    expect(state.value).toBe("ok");
    expect(errorMessage.value).toBeNull();
    expect(lastOkAt.value).not.toBeNull();
  });

  // The honesty invariant for live polling: a transient failure must not blank a
  // known-good snapshot. The rail keeps showing the real last value, labelled
  // stale via lastOkAt, rather than pretending it never had data.
  it("keeps the last snapshot and switches to error on a transient failure", async () => {
    fetchStatus.mockResolvedValueOnce(OK);
    await refresh();
    expect(status.value).toBe(OK);

    fetchStatus.mockRejectedValueOnce(new Error("fetch failed"));
    await refresh();
    expect(state.value).toBe("error");
    expect(errorMessage.value).toMatch(/fetch failed/);
    expect(status.value).toBe(OK);
  });

  it("reports an error with no snapshot on a first-load failure", async () => {
    fetchStatus.mockRejectedValueOnce(new Error("down"));
    await refresh();
    expect(state.value).toBe("error");
    expect(status.value).toBeNull();
  });

  it("deduplicates concurrent calls into a single fetch", async () => {
    fetchStatus.mockResolvedValue(OK);
    const p1 = refresh();
    const p2 = refresh();
    await Promise.all([p1, p2]);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
  });
});

describe("start / stop polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("polls immediately and then on the interval", async () => {
    fetchStatus.mockResolvedValue(OK);
    start(30_000);
    // The immediate refresh runs on the microtask queue.
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchStatus).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(30_000);
    expect(fetchStatus).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(fetchStatus).toHaveBeenCalledTimes(3);
  });

  it("stops polling after stop()", async () => {
    fetchStatus.mockResolvedValue(OK);
    start(30_000);
    await vi.advanceTimersByTimeAsync(0);
    stop();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchStatus).toHaveBeenCalledTimes(1);
  });

  it("does not double-register when started twice", async () => {
    fetchStatus.mockResolvedValue(OK);
    start(30_000);
    start(30_000);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(30_000);
    // Immediate once + one interval tick = 2, not 3.
    expect(fetchStatus).toHaveBeenCalledTimes(2);
  });
});
