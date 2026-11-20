import { describe, expect, it, vi } from "vitest";
import type { Seat } from "../src/api";
import { AVAILABLE, HELD, SOLD, SeatGrid, SeatModel } from "../src/seat-model";

const seat = (idx: number, init?: Partial<Seat>): Seat => ({
  id: idx + 1, idx, zone: "A", row: "1", number: idx + 1, status: "available", version: 3, ...init,
});

describe("seat deltas", () => {
  it("advances the cursor without rolling back a seat changed before the snapshot", () => {
    const model = new SeatModel([seat(100), seat(200)], 10);
    const dirty = vi.fn();
    expect(model.apply({ from: 11, to: 12, changes: [[100, "held", 2], [200, "sold", 4]] }, dirty)).toBe("applied");
    expect([...model.states]).toEqual([AVAILABLE, SOLD]);
    expect([...model.versions]).toEqual([3, 4]);
    expect(model.applied).toBe(12);
    expect(dirty.mock.calls).toEqual([[1]]);
  });

  it("ignores equal versions and replays the unapplied part of an overlapping frame", () => {
    const model = new SeatModel([seat(0), seat(1)], 4);
    const dirty = vi.fn();
    expect(model.apply({ from: 3, to: 4, changes: [[0, "sold", 3]] }, dirty)).toBe("duplicate");
    expect(model.apply({ from: 4, to: 5, changes: [[0, "sold", 3], [1, "held", 4]] }, dirty)).toBe("applied");
    expect([...model.states]).toEqual([AVAILABLE, HELD]);
    expect(dirty.mock.calls).toEqual([[1]]);
  });

  it("preserves the last applied cursor and seat states after a gap", () => {
    const model = new SeatModel([seat(100)], 11);
    const dirty = vi.fn();
    expect(model.apply({ from: 13, to: 13, changes: [[100, "sold", 5]] }, dirty)).toBe("gap");
    expect(model.applied).toBe(11);
    expect(model.states[0]).toBe(AVAILABLE);
    expect(dirty).not.toHaveBeenCalled();
    expect(model.apply({ from: 12, to: 13, changes: [[100, "held", 4]] }, dirty)).toBe("applied");
    expect(model.applied).toBe(13);
  });

  it("rejects a frame from a different layout before applying any changes", () => {
    const model = new SeatModel([seat(5)], 2);
    const dirty = vi.fn();
    expect(model.apply({ from: 3, to: 3, changes: [[5, "held", 4], [6, "sold", 4]] }, dirty)).toBe("layout-mismatch");
    expect(model.versions[0]).toBe(3);
    expect(model.applied).toBe(2);
    expect(dirty).not.toHaveBeenCalled();
  });
});

describe("seat grid", () => {
  it("maps a cell to its seat across zones and rejects gutters and empty cells", () => {
    const grid = new SeatGrid([seat(0, { number: "15" }), seat(1, { number: "2" }), seat(2, { zone: "B" })]);
    expect(grid.hit(grid.x[1]! + 4, grid.y[1]! + 4)).toBe(1);
    expect(grid.hit(grid.x[0]! + 4, grid.y[0]! + 4)).toBe(0);
    expect(grid.hit(grid.x[0]! + 15, grid.y[0]! + 4)).toBeUndefined();
    expect(grid.hit(grid.x[2]! + 4, grid.y[2]! + 4)).toBe(2);
    expect(grid.hit(grid.x[0]!, grid.y[2]!)).toBeUndefined();
    expect(grid.hit(-10, -10)).toBeUndefined();
    expect(grid.neighbor(1, 1, 0)).toBe(0);
    expect(grid.neighbor(0, 0, 1)).toBe(2);
  });

  it("only visits visible seats in a 10,000-seat plan", () => {
    const seats = Array.from({ length: 10_000 }, (_, index) =>
      seat(index, { row: String(Math.floor(index / 50)), number: index % 50 + 1 }));
    const grid = new SeatGrid(seats);
    const visited = vi.fn();
    grid.visible(0, 0, 400, 500, () => {}, () => {}, visited);
    expect(visited.mock.calls.length).toBeGreaterThan(0);
    expect(visited.mock.calls.length).toBeLessThan(600);
    expect(grid.hit(grid.x[9999]!, grid.y[9999]!)).toBe(9999);
  });
});

it("keeps the newest known seat versions when a resync returns a cached snapshot", () => {
  const previous = new SeatModel([seat(4, { id: 120, status: "sold", version: 10 })], 20);
  const model = new SeatModel([seat(5, { id: 120, status: "held", version: 9 })], 19, previous);
  expect(model.versions[0]).toBe(10);
  expect(model.states[0]).toBe(SOLD);
  expect(model.applied).toBe(19);
});
