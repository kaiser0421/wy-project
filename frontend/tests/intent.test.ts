import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";
import type { Booking } from "../src/api";

let intents: typeof import("../src/intent");
let ApiError: typeof import("../src/api").ApiError;

beforeAll(async () => {
  vi.stubGlobal("location", new URL("http://localhost/"));
  intents = await import("../src/intent");
  ({ ApiError } = await import("../src/api"));
  vi.unstubAllGlobals();
});

beforeEach(() => {
  vi.stubGlobal("navigator", undefined);
  const storage = Object.create(null) as Record<string, string | undefined>;
  Object.defineProperties(storage, {
    getItem: { value: (key: string) => storage[key] ?? null },
    setItem: { value: (key: string, value: string) => { storage[key] = value; } },
    removeItem: { value: (key: string) => { delete storage[key]; } },
  });
  vi.stubGlobal("localStorage", storage);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("persists the exact booking before dispatch and restores it only for the original account", async () => {
  const scope = { operation: "booking", event: 7 } as const;
  const first = new intents.MutationIntent<Booking>(42, scope);
  const body = { seat_ids: [3, "8"] };
  const send = vi.fn((saved: Booking, key: string) => {
    expect(first.current).toMatchObject({ event: 7, body: saved, key });
    expect(typeof first.current?.timestamp).toBe("number");
    return Promise.reject(new TypeError("response lost"));
  });
  await expect(first.send(body, send)).rejects.toThrow("response lost");
  expect(intents.pendingIntents(43)).toEqual([]);
  const restored = new intents.MutationIntent<Booking>("42", scope);
  expect(intents.pendingIntents(42)).toEqual([restored.current]);
  const resume = vi.fn(() => Promise.resolve("order"));
  await expect(restored.send(undefined, resume)).resolves.toBe("order");
  expect(resume).toHaveBeenCalledWith(body, send.mock.calls[0]![1]);
  expect(intents.pendingIntents(42)).toEqual([]);
});

it("restores a payment after re-login, enforces Retry-After after reload and clears on a final error", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  const scope = { operation: "payment", event: 7, order: "some-order" } as const;
  const first = new intents.MutationIntent<null>(42, scope);
  await expect(first.send(null, () => Promise.reject(new ApiError(401, "unauthenticated", "Session ended"))))
    .rejects.toMatchObject({ status: 401 });
  const restored = new intents.MutationIntent<null>(42, scope);
  const key = restored.current!.key;
  expect(restored.current).toMatchObject({ ...scope, body: null });
  await expect(restored.send(undefined, () => Promise.reject(new ApiError(503, "server-busy", "Busy", undefined, true, "3"))))
    .rejects.toMatchObject({ status: 503 });
  const reloaded = new intents.MutationIntent<null>(42, scope);
  const final = vi.fn(() => Promise.reject(new ApiError(402, "payment-declined", "Declined")));
  expect(reloaded.seconds).toBe(3);
  await expect(reloaded.send(undefined, final)).rejects.toBeInstanceOf(intents.IntentUnavailable);
  expect(final).not.toHaveBeenCalled();
  vi.setSystemTime(Date.now() + 3000);
  await expect(reloaded.send(undefined, final)).rejects.toMatchObject({ status: 402 });
  expect(final).toHaveBeenCalledWith(null, key);
  expect(reloaded.current).toBeUndefined();
});

it("requires working storage and never silently replaces an expired request", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  const scope = { operation: "booking", event: 7 } as const;
  const original = new intents.MutationIntent<Booking>(42, scope);
  const dispatch = vi.fn(() => Promise.reject(new TypeError("offline")));
  await expect(original.send({ seat_ids: [12] }, dispatch)).rejects.toThrow("offline");
  vi.setSystemTime(Date.now() + 24 * 60 * 60 * 1000);
  await expect(original.send({ seat_ids: [14] }, dispatch)).rejects.toThrow("retry window");
  expect(original.current?.body).toEqual({ seat_ids: [12] });
  await original.dismissExpired();
  expect(original.current).toBeUndefined();
  vi.stubGlobal("localStorage", { getItem: () => { throw new DOMException("blocked", "SecurityError"); } });
  await expect(original.send({ seat_ids: [14] }, dispatch)).rejects.toThrow("browser storage");
  expect(dispatch).toHaveBeenCalledTimes(1);
});

it("interprets delta-seconds and HTTP-date Retry-After for throttled, busy and in-flight responses", () => {
  const now = Date.parse("2026-11-21T12:00:00Z");
  expect(intents.retryDelay(new ApiError(429, "rate-limited", "", undefined, false, "8"), now)).toBe(8000);
  expect(intents.retryDelay(new ApiError(503, "server-busy", "", undefined, true,
    "Sat, 21 Nov 2026 12:00:05 GMT"), now)).toBe(5000);
  expect(intents.retryDelay(new ApiError(409, "idempotency-in-progress", ""), now)).toBe(1000);
  expect(intents.retryDelay(new ApiError(409, "insufficient-seats", ""), now)).toBe(0);
  expect(intents.retrySeconds(now + 1001, now)).toBe(2);
});
