import { signal } from "@preact/signals";
import { describeError, getAuthToken } from "./api";
import { getSystemStatus, type SystemStatus } from "./system";

export type SystemState = "idle" | "loading" | "ok" | "error";

export const status = signal<SystemStatus | null>(null);
export const state = signal<SystemState>("idle");
export const errorMessage = signal<string | null>(null);
// Wall-clock of the last successful poll. When the status endpoint itself
// becomes unreachable, the rail keeps showing the last real snapshot but labels
// it with this timestamp so a glance can tell "stale but real" from "live".
export const lastOkAt = signal<number | null>(null);

const POLL_INTERVAL_MS = 30_000;

let timer: ReturnType<typeof setInterval> | null = null;
let currentPromise: Promise<void> | null = null;

export function refresh(): Promise<void> {
  if (currentPromise) return currentPromise;

  const token = getAuthToken();
  // No token => nothing to fetch. The rail only mounts when signed in, but
  // guarding here keeps the store safe if a caller races sign-out.
  if (!token) {
    return Promise.resolve();
  }

  state.value = status.value === null ? "loading" : state.value;

  currentPromise = getSystemStatus()
    .then((data) => {
      status.value = data;
      state.value = "ok";
      errorMessage.value = null;
      lastOkAt.value = Date.now();
    })
    .catch((err) => {
      const { message } = describeError(err);
      errorMessage.value = message;
      state.value = "error";
      // status.value is intentionally retained: a transient blip should not
      // blank a known-good snapshot. The rail labels it via lastOkAt.
    })
    .finally(() => {
      currentPromise = null;
    });

  return currentPromise;
}

export function start(intervalMs: number = POLL_INTERVAL_MS): void {
  if (timer !== null) return;
  void refresh();
  timer = setInterval(() => void refresh(), intervalMs);
}

export function stop(): void {
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
}

// Test-only: vitest reuses the module instance across cases, so the signals and
// timer must be returned to a clean state between tests. Not imported by app code.
export function __resetForTest(): void {
  stop();
  status.value = null;
  state.value = "idle";
  errorMessage.value = null;
  lastOkAt.value = null;
  currentPromise = null;
}
