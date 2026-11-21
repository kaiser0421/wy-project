import type { AccessLog, Alert, EventInfo, ImportJob, Metric, Order, RecentLog, Role, Seat, SeatChange, Session, Zone } from "./api";

const now = Date.now();
const iso = (time: number) => new Date(time).toISOString();
const zones: Zone[] = [
  { code: "A", name: "Main Floor", price_twd: 2400 },
  { code: "B", name: "East Terrace", price_twd: 1800 },
  { code: "C", name: "West Terrace", price_twd: 1800 },
  { code: "D", name: "Upper Circle", price_twd: 1200 },
];
const events: EventInfo[] = [{
  id: 1, owner_id: 2, title: "Harbor Lights Festival", category: "Music", venue: "Taipei Harbor Hall",
  description: "An evening of live music at Taipei Harbor Hall.", sales_start_at: iso(now - 3600000),
  sales_end_at: iso(now + 86400000), status: "published", zones,
}];
const seats: Seat[] = Array.from({ length: 10_000 }, (_, idx) => ({
  idx, id: idx + 1, zone: zones[Math.floor(idx / 2500)]!.code,
  row: String(1 + Math.floor((idx % 2500) / 50)), number: idx % 50 + 1,
  status: idx % 9 === 0 ? "sold" : idx % 7 === 0 ? "held" : "available", version: 1,
}));
const users = (["admin", "user", "viewer"] as const).map((role, index) => ({
  id: index + 1, role, email: `${role}@demo.test`, is_active: true, version: 1,
}));
const orders = new Map<string, Order>();
const bookings = new Map<string, string>();
const jobs = new Map<string, { data: ImportJob; started: number }>();
const revisions = new Map<string | number, number>();
const alerts: Alert[] = [{ id: 1, rule_id: "EDGE_P99", scope: "All traffic", state: "firing", ts: iso(now),
  value: 168, threshold: 150 }];
const recentLogs: RecentLog[] = [{ ts: iso(now - 30_000), service: "api", level: "warning",
  event: "request.db_overload", request_id: "demo-request-1", attrs: { error_type: "TimeoutError" } }];
const access: AccessLog = { ts: iso(now - 30_000), method: "GET", path: "/api/v1/events",
  status: 503, req_ms: 18, request_id: "demo-request-1" };
let seq = 0;
const replay: { from: number; to: number; changes: SeatChange[] }[] = [];

function update(changes: Seat[]): void {
  const from = seq + 1;
  replay.push({ from, to: ++seq, changes: changes.map(seat => [seat.idx, seat.status, ++seat.version]) });
  if (replay.length > 120) replay.shift();
}

function tick(): void {
  const updates: Seat[] = [];
  for (let index = 0; index < 36; index++) {
    const seat = seats[Math.floor(Math.random() * seats.length)]!;
    if (seat.idx < 2 || seat.status === "sold" || updates.includes(seat)
      || [...orders.values()].some(order => order.items?.some(item => item.seat_id === seat.id))) continue;
    seat.status = seat.status === "available" ? "held" : "available";
    updates.push(seat);
  }
  if (updates.length) update(updates);
}

function metric(time: number): Metric {
  const second = Math.floor(time / 1000);
  const wave = (Math.sin(second / 11) + 1) / 2;
  const spike = Math.pow(Math.max(0, Math.cos(second / 17)), 9);
  return {
    ts: second,
    edge: { rps: 520 + 290 * wave + 320 * spike, p50_ms: 13 + 10 * wave,
      p95_ms: 37 + 35 * wave, p99_ms: 68 + 95 * spike, error_rate: .002 + .016 * spike },
    app: { rps: 500 + 270 * wave + 300 * spike, p50_ms: 7 + 6 * wave,
      p95_ms: 24 + 26 * wave, p99_ms: 46 + 70 * spike, error_rate: .001 + .009 * spike },
    connections: { active: Math.round(690 + 260 * wave), sse: Math.round(390 + 200 * wave) },
    business: { hold_ok: Math.round(24 + 10 * wave), hold_conflict: Math.round(7 * spike), pay_ok: 16 },
    completeness: second % 51 < 4 ? .7 : 1,
  };
}

type Emit = (event: string, data: unknown, id?: number) => void;
function sse(init: RequestInit, every: number, send: (emit: Emit) => void): Response {
  const encoder = new TextEncoder();
  let cleanup = () => {};
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      const emit: Emit = (event, data, id) => controller.enqueue(encoder.encode(
        `${id === undefined ? "" : `id: ${id}\n`}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`,
      ));
      const close = () => {
        cleanup();
        controller.close();
      };
      const timer = setInterval(() => send(emit), every);
      cleanup = () => {
        clearInterval(timer);
        init.signal?.removeEventListener("abort", close);
      };
      if (init.signal?.aborted) close();
      else {
        init.signal?.addEventListener("abort", close, { once: true });
        send(emit);
      }
    },
    cancel() { cleanup(); },
  }), { headers: { "Content-Type": "text/event-stream" } });
}

const json = (body: unknown, revision?: number) => Response.json(body, {
  headers: revision === undefined ? undefined : { ETag: `"v${revision}"` },
});
const problem = (status: number, code: string, detail: string) => Response.json({ code, detail }, {
  status, headers: { "Content-Type": "application/problem+json" },
});
const empty = () => new Response(null, { status: 204 });
const list = (items: unknown[]) => json({ items, next_cursor: null });

function currentSession(): Session | undefined {
  const id = localStorage.getItem("ticketing-demo-user");
  const user = users.find(user => String(user.id) === id && user.is_active);
  return user ? { user, csrf_token: "local-demo" } : undefined;
}

function orderState(order: Order, status: string): void {
  order.status = status;
  const changed = order.items?.flatMap(item => seats.filter(seat => seat.id === item.seat_id)) ?? [];
  for (const seat of changed) seat.status = status === "paid" ? "sold" : "available";
  if (changed.length) update(changed);
}

function jsonBody<T>(init: RequestInit): T {
  if (typeof init.body !== "string") throw new SyntaxError("Expected a JSON request body.");
  return JSON.parse(init.body) as T;
}

function book(event: EventInfo, init: RequestInit): Response {
  const key = new Headers(init.headers).get("Idempotency-Key") ?? "";
  const existing = bookings.get(key);
  if (existing) return json(orders.get(existing));
  if (event.status !== "published") return problem(409, "event-not-on-sale", "This event is not open for booking.");
  const data = jsonBody<{ seat_ids?: number[]; zone_code?: string; quantity?: number }>(init);
  const inventory = event.id === 1 ? seats : [];
  const chosen = data.seat_ids ? inventory.filter(seat => data.seat_ids!.includes(Number(seat.id)))
    : inventory.filter(seat => seat.zone === data.zone_code && seat.status === "available").slice(0, data.quantity);
  const count = data.seat_ids?.length ?? data.quantity ?? 0;
  if (!count || chosen.length !== count || chosen.some(seat => seat.status !== "available")) {
    return problem(409, data.seat_ids ? "seat-unavailable" : "insufficient-seats", "Choose available seats.");
  }
  const subtotal = chosen.reduce((sum, seat) => sum + (zones.find(zone => zone.code === seat.zone)?.price_twd ?? 0), 0);
  const fee = Math.round(subtotal * .05);
  const id = crypto.randomUUID();
  const order: Order = { id, event_id: event.id, event_title: event.title, status: "pending_payment",
    items: chosen.map(seat => ({ seat_id: seat.id, zone_code: seat.zone, row_label: seat.row, seat_number: seat.number })),
    subtotal_twd: subtotal, fee_twd: fee, total_twd: subtotal + fee, expires_at: iso(Date.now() + 300_000) };
  chosen.forEach(seat => { seat.status = "held"; });
  update(chosen);
  orders.set(id, order);
  bookings.set(key, id);
  return json(order);
}

function mockEvent(path: string, verb: string | undefined, init: RequestInit, url: URL): Response | undefined {
  const [, , id, action] = path.split("/");
  const event = events.find(item => String(item.id) === id);
  if (!event) return undefined;
  const revision = revisions.get(event.id) ?? 1;
  if (verb === "PATCH" || action === "publish") {
    if (new Headers(init.headers).get("If-Match") !== `"v${revision}"`) {
      return problem(412, "precondition-failed", "Reload the current event before saving.");
    }
    if (action === "publish") event.status = "published";
    else Object.assign(event, jsonBody<Partial<EventInfo>>(init));
    revisions.set(event.id, revision + 1);
    return json(event, revision + 1);
  }
  if (verb === "DELETE") {
    events.splice(events.indexOf(event), 1);
    return empty();
  }
  if (action === "seats") return json({ event_id: event.id, layout_ver: 1, last_seq: event.id === 1 ? seq : 0,
    zones: event.zones ?? [], seats: event.id === 1 ? seats : [] });
  if (action === "seat-stream") {
    let applied = Number(new Headers(init.headers).get("Last-Event-ID") ?? seq);
    return sse(init, 1000, emit => {
      if (event.id !== 1) {
        emit("progress", { head: 0, oldest_unassigned_age_ms: 0 });
        return;
      }
      tick();
      for (const frame of replay) if (frame.to > applied) emit("delta", frame, frame.to);
      applied = seq;
      emit("progress", { head: seq, oldest_unassigned_age_ms: 0 });
    });
  }
  if (action === "orders") return book(event, init);
  if (action === "zones") return json(event.zones ?? []);
  if (action === "reports") return problem(501, "demo-report", "Sales exports are available with the connected API.");
  if (action === "seat-imports") {
    const data: ImportJob = { id: crypto.randomUUID(), status: "queued", total_rows: 50,
      processed_rows: 0, valid_rows: 0, error_rows: 0 };
    jobs.set(data.id, { data, started: Date.now() });
    return json(data);
  }
  if (url.pathname === `/events/${event.id}`) return json(event, revision);
  return undefined;
}

/** Simulates the network boundary so demo routes use the same client, SSE parser and UI. */
export function mock(resource: string, init: RequestInit): Response {
  const url = new URL(resource, location.origin);
  const path = url.pathname;
  const verb = init.method;
  if (path === "/auth/login") {
    const { email } = jsonBody<{ email: string }>(init);
    const role: Role = email.startsWith("viewer") ? "viewer" : email.startsWith("admin") ? "admin" : "user";
    const account = users.find(user => user.email === `${role}@demo.test`)!;
    localStorage.setItem("ticketing-demo-user", String(account.id));
    return json(currentSession());
  }
  if (path === "/auth/logout") {
    localStorage.removeItem("ticketing-demo-user");
    return empty();
  }
  const session = currentSession();
  if (!session) return problem(401, "authentication-required", "Sign in to continue.");
  if (path === "/auth/me") return json(session);
  if (path === "/events") {
    if (verb === "POST") {
      const body = jsonBody<Omit<EventInfo, "id" | "status">>(init);
      const event = { ...body, id: crypto.randomUUID(), owner_id: session.user.id, status: "draft" };
      events.push(event);
      return json(event, 1);
    }
    const needle = url.searchParams.get("q")?.toLowerCase() ?? "";
    const category = url.searchParams.get("category")?.toLowerCase() ?? "";
    const matches = events.filter(event => [event.title, event.venue, event.description].some(value => value.toLowerCase().includes(needle))
      && (!category || event.category.toLowerCase() === category)
      && (event.status === "published" || session.user.role === "admin" || event.owner_id === session.user.id));
    const sort = url.searchParams.get("sort");
    if (sort === "-created") matches.reverse();
    if (sort === "sales_start_at") matches.sort((a, b) => a.sales_start_at.localeCompare(b.sales_start_at));
    if (sort === "price_min") matches.sort((a, b) => minPrice(a) - minPrice(b));
    return list(matches);
  }
  if (path.startsWith("/events/")) {
    const response = mockEvent(path, verb, init, url);
    if (response) return response;
  }
  if (path === "/orders") return list([...orders.values()].reverse());
  if (path.startsWith("/orders/")) {
    const [, , id, action] = path.split("/");
    const order = id ? orders.get(id) : undefined;
    if (!order) return problem(404, "not-found", "Order not found.");
    if (order.status === "pending_payment" && Date.parse(order.expires_at ?? "") <= Date.now()) orderState(order, "expired");
    if (action === "cancel") {
      orderState(order, "cancelled");
      return empty();
    }
    if (action === "payments") {
      setTimeout(() => { if (order.status === "pending_payment") orderState(order, "paid"); }, 1500);
      return json({ expires_at: order.expires_at });
    }
    return json(order);
  }
  if (path.startsWith("/seat-imports/")) {
    const job = jobs.get(path.split("/")[2] ?? "");
    if (job) {
      const processed = Math.min(50, Math.floor((Date.now() - job.started) / 60));
      return json({ ...job.data, processed_rows: processed, valid_rows: processed,
        status: processed === 50 ? "succeeded" : "running" });
    }
  }
  if (path === "/admin/metrics") {
    const last = Date.parse(url.searchParams.get("to") ?? "");
    const first = Date.parse(url.searchParams.get("from") ?? "");
    return list(Array.from({ length: Math.max(0, Math.min(300, Math.ceil((last - first) / 1000))) },
      (_, index) => metric(last - (index + 1) * 1000)));
  }
  if (path === "/admin/metrics/stream") return sse(init, 1000, emit => emit("metric", metric(Date.now())));
  if (path === "/admin/alerts") return list(alerts);
  if (path === "/admin/alerts/stream") return sse(init, 1000, emit => {
    for (const alert of alerts) emit("alert", alert);
  });
  if (path === "/admin/alerts/1/ack") {
    alerts[0]!.ack_at = iso(Date.now());
    return json(alerts[0]);
  }
  if (path === "/admin/logs/recent") {
    if (session.user.role !== "admin") return problem(403, "forbidden", "Admin access is required.");
    const { service, level, since_minutes, limit } = Object.fromEntries(url.searchParams);
    const cutoff = Date.now() - Number(since_minutes ?? 15) * 60_000;
    return list(recentLogs.filter(entry => (!service || entry.service === service) && (!level || entry.level === level)
      && Date.parse(entry.ts) >= cutoff).slice(0, Number(limit ?? 50)));
  }
  if (path === "/admin/logs/access") {
    if (session.user.role !== "admin") return problem(403, "forbidden", "Admin access is required.");
    const { status_class, method, path_prefix, request_id, since_minutes } = Object.fromEntries(url.searchParams);
    const cutoff = Date.now() - Number(since_minutes ?? 15) * 60_000;
    return list((!status_class || status_class === "5xx") && (!method || method.toUpperCase() === access.method)
      && (!path_prefix || access.path?.startsWith(path_prefix)) && (!request_id || access.request_id === request_id)
      && Date.parse(access.ts) >= cutoff ? [access] : []);
  }
  if (path === "/admin/requests/demo-request-1/logs") return json({ access_log: access, service_logs: recentLogs });
  if (path.startsWith("/admin/requests/")) return json({ entries: [
    { ts: iso(Date.now()), service: "edge", method: "GET", path: "/api/v1/events", status: 200, req_ms: 18 },
    { ts: iso(Date.now()), service: "api", level: "info", event: "request_finished" },
  ] });
  if (path === "/admin/users") return list(users);
  if (path.startsWith("/admin/users/")) {
    const user = users.find(user => String(user.id) === path.split("/").at(-1));
    if (user && verb === "PATCH") {
      if (new Headers(init.headers).get("If-Match") !== `"v${user.version}"`) {
        return problem(412, "precondition-failed", "Reload the current user before saving.");
      }
      Object.assign(user, jsonBody<Partial<typeof user>>(init), { version: user.version + 1 });
    }
    if (user) return json(user, user.version);
  }
  return problem(404, "not-found", "This record was not found.");
}

const minPrice = (event: EventInfo) => Math.min(...(event.zones ?? []).map(zone => zone.price_twd));
