import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

let api: typeof import("../src/api").api;
const signal = new AbortController().signal;
const ts = "2026-11-20T12:00:00Z";

beforeAll(async () => {
  vi.stubGlobal("location", new URL("http://localhost/"));
  ({ api } = await import("../src/api"));
  vi.unstubAllGlobals();
});

afterEach(() => vi.unstubAllGlobals());

function respond(body: unknown): void {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(body)));
}

function stream(event: string, data: unknown): void {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`, {
    headers: { "Content-Type": "text/event-stream" },
  })));
}

async function collect<T>(frames: AsyncIterable<T>): Promise<T[]> {
  const values: T[] = [];
  for await (const frame of frames) values.push(frame);
  return values;
}

describe("API payloads", () => {
  it("normalizes the public order ID for creation, listing and detail", async () => {
    const order = { public_id: "public-uuid", status: "pending_payment" };
    respond(order);
    await expect(api.book(1, { seat_ids: [2] }, "key", signal)).resolves.toMatchObject({ id: order.public_id });
    await expect(api.order(order.public_id, signal)).resolves.toMatchObject({ id: order.public_id });
    respond({ items: [order], next_cursor: "12" });
    await expect(api.orders(null, signal)).resolves.toMatchObject({ items: [{ id: order.public_id }], next_cursor: "12" });
  });

  it("reads import progress before row counts are available", async () => {
    respond({ id: "job", progress_percent: 1, total_rows: null });
    await expect(api.importJob("job", signal)).resolves.toMatchObject({ progress: .01 });
  });

  it("joins edge and app history by second and maps connection gauges", async () => {
    respond({ items: [
      { ts, source: "edge", req_total: 300, conns_active: 12 },
      { ts, source: "app", req_total: 280, conns_sse: 7, completeness: .75 },
    ] });
    await expect(api.metrics(ts, ts, signal)).resolves.toMatchObject([{
      ts, edge: { rps: 300 }, app: { rps: 280 }, connections: { active: 12, sse: 7 }, completeness: .75,
    }]);
  });

  it("aggregates business metrics and accepts the published SSE event names", async () => {
    stream("metrics", { ts, edge: { req_total: 300 }, app: { req_total: 280 }, complete: true,
      business: [{ hold_ok: 4, pay_ok: 3 }, { hold_ok: 2, pay_ok: 1 }] });
    await expect(collect(api.metricsStream(signal))).resolves.toMatchObject([{
      edge: { rps: 300 }, app: { rps: 280 }, completeness: 1, business: { hold_ok: 6, pay_ok: 4 },
    }]);
    stream("alerts", { items: [{ id: 2, state: "firing" }] });
    await expect(collect(api.alertsStream(signal))).resolves.toEqual([{ snapshot: [{ id: 2, state: "firing" }] }]);
  });

  it("combines access and service entries in chronological order", async () => {
    respond({ access_log: { ts, status: 200 }, service_logs: [{ ts: "2026-11-20T11:59:59Z", service: "api" }] });
    await expect(api.trace("request", signal)).resolves.toMatchObject([{ service: "api" }, { service: "nginx", status: 200 }]);
    respond([{ ts, service: "worker" }]);
    await expect(api.trace("request", signal)).resolves.toMatchObject([{ service: "worker" }]);
  });
});
