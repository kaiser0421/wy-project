import { expect, it } from "vitest";
import { countdown, money } from "../src/format";
import { incomplete, MetricWindow } from "../src/metrics";
import { EventParser, readFrames } from "../src/sse";

it("parses fragmented CRLF, comments, and multiline SSE data", () => {
  const parser = new EventParser();
  expect(parser.push(": keepalive\r\nid: 17\r")).toEqual([]);
  expect(parser.push("\nevent: del")).toEqual([]);
  expect(parser.push("ta\rdata: one\r\ndata: two\r\n\r")).toEqual([
    { id: "17", event: "delta", data: "one\ntwo" },
  ]);
  expect(parser.push("\ndata: {}\n\n")).toEqual([{ event: "message", data: "{}" }]);
});

it("decodes UTF-8 split across transport chunks", async () => {
  const bytes = new TextEncoder().encode("data: 座位\n\n");
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes.slice(0, 7));
      controller.enqueue(bytes.slice(7));
      controller.close();
    },
  });
  const frames = [];
  for await (const frame of readFrames(stream)) frames.push(frame);
  expect(frames).toEqual([{ event: "message", data: "座位" }]);
});

it("formats integer TWD and rounds a running countdown up", () => {
  expect(money(12345)).toBe("NT$12,345");
  expect(money(0)).toBe("NT$0");
  expect(countdown("2026-01-01T00:01:01.1Z", Date.parse("2026-01-01T00:00:00Z"))).toBe("1:02");
  expect(countdown("2026-01-01T00:01:01.1Z", Date.parse("2026-01-01T00:02:00Z"))).toBe("0:00");
});

it("aligns a 300-second window, preserving newer live samples over backfill", () => {
  const window = new MetricWindow();
  window.add({ ts: 1700000000, completeness: 1, edge: { rps: 7 } });
  window.add({ ts: 1700000000000, completeness: 0, edge: { rps: 0 } }, false);
  window.add({ ts: 1699999998, completeness: 0.8 });
  const { seconds, samples } = window.window(1700000000);
  expect(seconds).toHaveLength(300);
  expect(seconds.slice(-3)).toEqual([1699999998, 1699999999, 1700000000]);
  expect(samples.slice(-3).map(incomplete)).toEqual([true, true, false]);
  expect(samples.at(-1)?.edge?.rps).toBe(7);
});

it("fills an incomplete live sample with more complete history and keeps sales activity", () => {
  const window = new MetricWindow();
  window.add({ ts: 1700000000, completeness: .5, business: { hold_ok: 2 } });
  window.add({ ts: 1700000000, completeness: 1, edge: { rps: 60 } }, false);
  expect(window.window(1700000000).samples.at(-1)).toMatchObject({
    completeness: 1, business: { hold_ok: 2 }, edge: { rps: 60 },
  });
});

it("buckets each source and its connection gauge using its own time and fills a historical gap", () => {
  const window = new MetricWindow();
  const ts = 1700000010;
  const delay = { ts: ts - 10, rps: 600 };
  window.add({ ts, edge: delay, app: { ts, rps: 11, completeness: 1 },
    connections: { active: 23, sse: 2 }, business: { hold_ok: 1 }, completeness: 1 });
  window.add({ ts: ts + 1, edge: delay, app: { ts: ts + 1, rps: 12 },
    connections: { active: 23, sse: 3 } });
  window.add({ ts: ts - 10, app: { rps: 7 }, connections: { sse: 4 }, completeness: 1 }, false);
  const buckets = window.window(ts + 1).samples;
  expect(buckets.at(-12)).toMatchObject({ edge: { rps: 600 }, app: { rps: 7 },
    connections: { active: 23, sse: 4 } });
  expect(buckets.at(-11)).toBeNull();
  expect(buckets.at(-2)).toMatchObject({ app: { rps: 11 }, business: { hold_ok: 1 }, connections: { sse: 2 } });
  expect(buckets.at(-2)?.connections?.active).toBeUndefined();
  expect(buckets.at(-1)?.edge).toBeUndefined();
});
