import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let api: typeof import("../src/api").api;
let ApiError: typeof import("../src/api").ApiError;
let onSessionExpired: typeof import("../src/api").onSessionExpired;
let seatStream: typeof import("../src/api").seatStream;
let retryDelay: typeof import("../src/intent").retryDelay;
const fetchMock = vi.fn<typeof fetch>();
const signal = new AbortController().signal;
const ts = "2026-11-20T12:00:00Z";
const session = { user: { id: 42, email: "user@example.test", role: "user" }, csrf_token: "csrf-token" };

beforeEach(async () => {
  vi.resetModules();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("location", new URL("http://localhost/"));
  ({ api, ApiError, onSessionExpired, seatStream } = await import("../src/api"));
  ({ retryDelay } = await import("../src/intent"));
});

afterEach(() => vi.unstubAllGlobals());

function respond(body: unknown): void {
  fetchMock.mockImplementation(() => Promise.resolve(Response.json(body)));
}

function stream(event: string, data: unknown): void {
  fetchMock.mockResolvedValueOnce(new Response(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`, {
    headers: { "Content-Type": "text/event-stream" },
  }));
}

function problem(status: number, body: object, headers?: Record<string, string>): void {
  fetchMock.mockResolvedValueOnce(Response.json(body, {
    status, headers: { ...headers, "Content-Type": "application/problem+json; charset=utf-8" },
  }));
}

async function collect<T>(frames: AsyncIterable<T>): Promise<T[]> {
  const values: T[] = [];
  for await (const frame of frames) values.push(frame);
  return values;
}

describe("API requests", () => {
  it("sends CSRF on unsafe requests and clears it on logout", async () => {
    respond(session);
    await api.login(session.user.email, "password", signal);
    await api.me(signal);
    await api.updateEvent("event/1", { title: "New title" }, '"v2"', signal);
    await api.deleteEvent("event/1", signal);
    await api.logout();
    await api.register(session.user.email, "password", signal);

    const requests = fetchMock.mock.calls.map(([url, init]) => ({ url, ...init, headers: new Headers(init?.headers) }));
    expect(requests.map(({ method, headers }) => [method, headers.get("X-CSRF-Token")])).toEqual([
      ["POST", ""], ["GET", null], ["PATCH", "csrf-token"], ["DELETE", "csrf-token"], ["POST", "csrf-token"], ["POST", ""],
    ]);
    expect(requests[2]).toMatchObject({
      url: "/api/v1/events/event%2F1", body: '{"title":"New title"}', credentials: "same-origin", cache: "no-store",
    });
    expect(requests[2]?.headers.get("Content-Type")).toBe("application/json");
    expect(requests[2]?.headers.get("If-Match")).toBe('"v2"');
  });

  it("preserves caller-supplied idempotency keys for booking, payment, cancellation and replay", async () => {
    respond({ id: "order/1" });
    const booking = { seat_ids: [2] };
    await api.book(1, booking, "book-key", signal);
    await api.book(1, booking, "book-key", signal);
    await api.pay("order/1", "pay-key", signal);
    await api.cancel("order/1", "cancel-key", signal);
    await api.order("order/1", signal);

    expect(fetchMock.mock.calls.map(([url, init]) => [url, new Headers(init?.headers).get("Idempotency-Key"), init?.body]))
      .toEqual([
        ["/api/v1/events/1/orders", "book-key", '{"seat_ids":[2]}'],
        ["/api/v1/events/1/orders", "book-key", '{"seat_ids":[2]}'],
        ["/api/v1/orders/order%2F1/payments", "pay-key", undefined],
        ["/api/v1/orders/order%2F1/cancel", "cancel-key", undefined],
        ["/api/v1/orders/order%2F1", null, undefined],
      ]);
  });

  it("lets fetch set the multipart boundary and reads upload progress", async () => {
    respond({ id: "job", progress: 0.5 });
    const file = new File(["seat,row"], "seats.csv");
    await expect(api.upload(1, file, "skip_invalid", signal)).resolves.toMatchObject({ id: "job", progress: 0.5 });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/v1/events/1/seat-imports");
    expect(new Headers(init?.headers).has("Content-Type")).toBe(false);
    const body = init?.body;
    if (!(body instanceof FormData)) throw new Error("Expected multipart form data");
    expect(body.get("file")).toBe(file);
    expect(body.get("mode")).toBe("skip_invalid");
  });
});

describe("API errors", () => {
  it("maps problem details, including field errors and the body request ID", async () => {
    const errors = [{ loc: ["body", "email"], msg: "Invalid address" }];
    problem(422, { code: "validation-error", detail: "Invalid input", request_id: "body-id", errors },
      { "X-Request-ID": "header-id" });
    const result = api.register("bad-address", "password", signal);
    await expect(result).rejects.toBeInstanceOf(ApiError);
    await expect(result).rejects.toMatchObject({
      status: 422, code: "validation-error", message: "Invalid input", requestId: "body-id", retryable: false, errors,
    });
  });

  it("expires the session on a protected 401, while an optional 401 preserves it", async () => {
    const expired = vi.fn();
    onSessionExpired(expired);
    respond(session);
    await api.me(signal);
    problem(401, { code: "invalid-credentials" });
    await expect(api.login(session.user.email, "wrong", signal)).rejects.toMatchObject({ status: 401 });
    expect(expired).not.toHaveBeenCalled();
    await api.publish(1, '"v2"', signal);
    expect(new Headers(fetchMock.mock.lastCall?.[1]?.headers).get("X-CSRF-Token")).toBe("csrf-token");

    problem(401, { code: "authentication-required" });
    await expect(api.events({}, signal)).rejects.toMatchObject({ status: 401, code: "authentication-required" });
    expect(expired).toHaveBeenCalledOnce();
    await api.publish(1, '"v2"', signal);
    expect(new Headers(fetchMock.mock.lastCall?.[1]?.headers).get("X-CSRF-Token")).toBe("");
  });

  it.each(["text/plain", "application/problem+json"])("falls back to HTTP details on a %s error body", async type => {
    fetchMock.mockResolvedValueOnce(new Response("Forbidden", {
      status: 403, headers: { "Content-Type": type, "X-Request-ID": "header-id" },
    }));
    const expired = vi.fn();
    onSessionExpired(expired);
    await expect(api.users(null, signal)).rejects.toMatchObject({
      status: 403, code: "http-error", message: "Request failed (HTTP 403).", requestId: "header-id", errors: [],
    });
    expect(expired).not.toHaveBeenCalled();
  });

  it.each([
    { status: 409, code: "idempotency-in-progress", retryAfter: "3", delay: 3000 },
    { status: 503, code: "server-busy", retryAfter: "Fri, 20 Nov 2026 12:00:05 GMT", delay: 5000 },
    { status: 503, code: "server-busy", retryAfter: "invalid", delay: 1000 },
  ])("maps $status $code and interprets Retry-After '$retryAfter'", async ({ status, code, retryAfter, delay }) => {
    problem(status, { code, detail: "Try again later", retryable: true }, { "Retry-After": retryAfter });
    const result = api.book(1, { seat_ids: [2] }, "same-key", signal);
    await expect(result).rejects.toMatchObject({ status, code, retryable: true, retryAfter });
    const error: unknown = await result.catch((reason: unknown) => reason);
    expect(retryDelay(error, Date.parse(ts))).toBe(delay);
  });

  it("propagates fetch failures and abort reasons without expiring the session", async () => {
    const expired = vi.fn();
    onSessionExpired(expired);
    const offline = new TypeError("Failed to fetch");
    fetchMock.mockRejectedValueOnce(offline);
    await expect(api.event(1, signal)).rejects.toBe(offline);

    const controller = new AbortController();
    fetchMock.mockImplementationOnce(() => {
      controller.abort();
      return Promise.resolve(new Response(null, { status: 401 }));
    });
    await expect(api.event(1, controller.signal)).rejects.toBe(controller.signal.reason as unknown);
    expect(expired).not.toHaveBeenCalled();
  });
});

describe("API payloads", () => {
  it("returns the record revision and rejects a response without an ETag", async () => {
    const data = { id: 3, title: "Event" };
    fetchMock.mockResolvedValueOnce(Response.json(data, { headers: { ETag: '"v3"' } }));
    await expect(api.eventRevision(3, signal)).resolves.toEqual({ data, etag: '"v3"' });
    respond({ id: 42 });
    await expect(api.user(42, signal)).rejects.toThrow("The server did not supply a revision");
  });

  it("omits empty filters and normalizes zone and seat snapshot payloads", async () => {
    respond({ items: [], next_cursor: null });
    await api.events({ limit: 0, cursor: null, category: "", q: "rock & roll" }, signal);
    expect(fetchMock.mock.lastCall?.[0]).toBe("/api/v1/events?limit=0&q=rock+%26+roll");

    const zones = [{ code: "A", name: "Zone A", price_twd: 100 }];
    respond({ zones });
    await expect(api.zones(1, signal)).resolves.toEqual(zones);
    respond(zones);
    await expect(api.zones(1, signal)).resolves.toEqual(zones);
    respond({ last_assigned_seq: 17, seats: [], zones });
    await expect(api.seats(1, 2, signal)).resolves.toMatchObject({ last_seq: 17 });
    expect(fetchMock.mock.lastCall?.[0]).toBe("/api/v1/events/1/seats?min_layout_ver=2");
  });

  it("resumes the seat stream at the last applied event and decodes its frames", async () => {
    const delta = { from: 18, to: 19, changes: [] };
    const layout = { layout_ver: 2 };
    const progress = { assigned_head: 19, oldest_unassigned_age_ms: 0 };
    fetchMock.mockResolvedValueOnce(new Response([
      ": keepalive", "", "event: delta", `data: ${JSON.stringify(delta)}`, "",
      "event: layout", `data: ${JSON.stringify(layout)}`, "",
      "event: progress", `data: ${JSON.stringify(progress)}`, "",
      "event: resync", "data: {}", "", "event: heartbeat", "data: {}", "", "",
    ].join("\n"), { headers: { "Content-Type": "text/event-stream" } }));
    const onComment = vi.fn();
    await expect(collect(seatStream(1, 17, signal, onComment))).resolves.toEqual([
      { event: "delta", data: delta }, { event: "layout", data: layout },
      { event: "progress", data: { ...progress, head: 19 } }, { event: "resync" }, { event: "heartbeat" },
    ]);
    expect(onComment).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/v1/events/1/seat-stream");
    expect(init?.signal).toBe(signal);
    const headers = new Headers(init?.headers);
    expect(headers.get("Last-Event-ID")).toBe("17");
    expect(headers.get("Accept")).toBe("text/event-stream");
  });

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
    const edgeTs = "2026-11-20T11:59:50Z";
    stream("metrics", { ts, edge: { ts: edgeTs, req_total: 300 }, app: { ts, req_total: 280 }, complete: true,
      business: [{ hold_ok: 4, pay_ok: 3 }, { hold_ok: 2, pay_ok: 1 }] });
    await expect(collect(api.metricsStream(signal))).resolves.toMatchObject([{
      edge: { ts: edgeTs, rps: 300 }, app: { ts, rps: 280 }, completeness: 1, business: { hold_ok: 6, pay_ok: 4 },
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
