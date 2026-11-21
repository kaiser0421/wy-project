import { expect, it } from "vitest";
import { SeatReconnect } from "../src/reconnect";

it("backs off repeated resyncs, even after an isolated progress frame", () => {
  const reconnect = new SeatReconnect();
  const delays = Array.from({ length: 7 }, (_, index) => {
    reconnect.caughtUp(index * 1000);
    return reconnect.delay(0);
  });
  expect(delays).toEqual([400, 800, 1600, 3200, 6000, 6000, 6000]);
  expect(reconnect.delay(1)).toBe(6000);
});

it("resets only after sustained catch-up", () => {
  const reconnect = new SeatReconnect();
  expect(reconnect.delay(0)).toBe(400);
  reconnect.caughtUp(0);
  reconnect.lagging();
  reconnect.caughtUp(10_000);
  expect(reconnect.delay(0)).toBe(800);
  reconnect.caughtUp(11_000);
  reconnect.caughtUp(21_000);
  expect(reconnect.delay(0)).toBe(400);
});
