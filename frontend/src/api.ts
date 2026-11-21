import { readFrames, type Frame } from "./sse";

const prefix = "/api/v1";
export const mockMode = new URLSearchParams(location.search).get("mock") === "1";
export type Id = string | number;
export type Role = "admin" | "user" | "viewer";
export interface User { id: Id; email: string; role: Role }
export interface AdminUser extends User { is_active: boolean; version: number }
export interface Session { user: User; csrf_token: string }
export interface Page<T> { items: T[]; next_cursor: string | null }
export interface Zone { code: string; name: string; price_twd: number }
export interface EventInput {
  title: string;
  description: string;
  category: string;
  venue: string;
  sales_start_at: string;
  sales_end_at: string;
}
export interface EventInfo extends EventInput {
  id: Id;
  status: string;
  owner_id?: Id;
  zones?: Zone[];
  unavailable?: false;
}
export type EventListItem = EventInfo | { id: Id; unavailable: true };
export type SeatStatus = "available" | "held" | "sold";
export interface Seat {
  idx: number;
  id: Id;
  zone: string;
  row: string;
  number: Id;
  status: SeatStatus;
  version: number;
}
export interface Snapshot {
  event_id: Id;
  layout_ver: number;
  last_seq: number;
  zones: Zone[];
  seats: Seat[];
}
export type SeatChange = [idx: number, status: SeatStatus, version: number];
export interface Delta { from: number; to: number; changes: SeatChange[] }
export type SeatFrame =
  | { event: "delta"; data: Delta }
  | { event: "layout"; data: { layout_ver: number } }
  | { event: "progress"; data: { head: number; oldest_unassigned_age_ms: number } }
  | { event: "resync" }
  | { event: "heartbeat" };
export type Booking = { seat_ids: Id[] } | { zone_code: string; quantity: number };
export interface OrderItem { seat_id?: Id; zone_code?: string; row_label?: string; seat_number?: Id }
export interface Order {
  id: string;
  event_id: Id;
  event_title?: string;
  items?: OrderItem[];
  status: string;
  subtotal_twd: number;
  fee_twd: number;
  total_twd: number;
  expires_at: string | null;
}
export const orderStatuses = ["pending_payment", "paid", "expired", "cancelled", "refund_pending", "refunded"] as const;
export type OrderStatus = typeof orderStatuses[number];
export interface AdminOrder {
  public_id: string;
  event_id: Id;
  event_title: string;
  buyer_email: string;
  status: OrderStatus;
  quantity: number;
  total_twd: number | null;
  created_at: string;
  expires_at: string | null;
}
export interface AdminOrderFilters {
  event_id?: Id;
  status?: string;
  buyer_email?: string;
  created_from?: string;
  created_to?: string;
}
export interface Checkout { checkout_url?: string; expires_at?: string }
export type ImportMode = "all_or_nothing" | "skip_invalid";
export interface ImportJob {
  id: string;
  status: string;
  processed_rows: number;
  total_rows: number | null;
  valid_rows: number | null;
  error_rows: number | null;
  progress?: number | null;
  failure_reason?: string | null;
}
export interface HttpMetrics {
  ts?: string | number;
  rps?: number | null;
  p50_ms?: number | null;
  p95_ms?: number | null;
  p99_ms?: number | null;
  error_rate?: number | null;
  err_5xx?: number;
  err_4xx?: number;
  completeness?: number | null;
}
export interface Metric {
  ts: string | number;
  edge?: HttpMetrics | null;
  app?: HttpMetrics | null;
  connections?: { active?: number | null; sse?: number | null } | null;
  business?: { hold_ok?: number; hold_conflict?: number; hold_soldout?: number; pay_ok?: number } | null;
  completeness?: number | null;
}
export interface Alert {
  id: Id;
  rule_id: string;
  scope?: string;
  state: string;
  ts: string;
  value?: number | null;
  threshold?: number | null;
  ack_at?: string | null;
  ack_by?: Id | null;
}
export interface TraceEntry {
  ts?: string;
  service?: string;
  level?: string;
  event?: string;
  method?: string;
  route?: string;
  path?: string;
  status?: number;
  req_ms?: number;
  attrs?: Record<string, unknown>;
}
export const logServices = ["api", "worker", "relay", "collector", "paymock", "seed"] as const;
type LogService = typeof logServices[number];
type LogLevel = "warning" | "error" | "critical";
export interface RecentLog {
  ts: string;
  service: string;
  level: LogLevel;
  event: string | null;
  request_id: string | null;
  attrs: Record<string, string | number>;
}
interface RecentLogFilters { service?: LogService; level?: LogLevel; since_minutes?: number; limit?: number }
type OrderPayload = Omit<Order, "id"> & ({ id: string } | { public_id: string });
type ImportPayload = ImportJob & { progress_percent?: number };
type MetricReading = HttpMetrics & {
  req_total?: number;
  conns_active?: number | null;
  conns_sse?: number | null;
};
interface LiveMetric extends Omit<Metric, "business"> {
  edge?: MetricReading | null;
  app?: MetricReading | null;
  business?: Metric["business"] | NonNullable<Metric["business"]>[];
  complete?: boolean;
}
type HistoryMetric = LiveMetric | (MetricReading & { ts: Metric["ts"]; source: "edge" | "app" });
export type AlertUpdate = { snapshot: Alert[] } | { alert: Alert };
interface RequestLogs { access_log: TraceEntry | null; service_logs: TraceEntry[] }
export interface FieldError { loc: (string | number)[]; msg: string }
interface Problem { code?: string; detail?: string; request_id?: string; retryable?: boolean; errors?: FieldError[] }

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    detail: string,
    public readonly requestId?: string,
    public readonly retryable = false,
    public readonly retryAfter?: string,
    public readonly errors: FieldError[] = [],
  ) {
    super(detail);
  }
}

let csrf = "";
let sessionExpired: () => void = () => undefined;

export function onSessionExpired(callback: () => void): void {
  sessionExpired = callback;
}

interface Options {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  authOptional?: boolean;
  timeoutMs?: number | null;
}

async function request(path: string, options: Options = {}): Promise<Response> {
  const { method = "GET", body, signal, authOptional, headers: extra, timeoutMs = 20_000 } = options;
  const headers = new Headers(extra);
  if (method !== "GET") headers.set("X-CSRF-Token", csrf);
  const multipart = body instanceof FormData;
  if (body !== undefined && !multipart) headers.set("Content-Type", "application/json");
  const timeout = timeoutMs === null ? undefined : AbortSignal.timeout(timeoutMs);
  const requestSignal = signal && timeout ? AbortSignal.any([signal, timeout]) : timeout ?? signal;
  const init: RequestInit = {
    method, headers, signal: requestSignal, credentials: "same-origin", cache: "no-store",
    body: multipart ? body : body === undefined ? undefined : JSON.stringify(body),
  };
  const response = mockMode ? (await import("./mock")).mock(path, init) : await fetch(prefix + path, init);
  signal?.throwIfAborted();
  if (response.ok) return response;
  let problem: Problem = {};
  if (response.headers.get("Content-Type")?.includes("application/problem+json")) {
    problem = await response.json().catch(() => ({})) as Problem;
  }
  signal?.throwIfAborted();
  if (response.status === 401 && !authOptional) {
    csrf = "";
    sessionExpired();
  }
  throw new ApiError(
    response.status, problem.code ?? "http-error", problem.detail ?? `Request failed (HTTP ${response.status}).`,
    problem.request_id ?? response.headers.get("X-Request-ID") ?? undefined,
    problem.retryable ?? false, response.headers.get("Retry-After") ?? undefined, problem.errors,
  );
}

async function json<T>(path: string, options?: Options): Promise<T> {
  return (await request(path, options)).json() as Promise<T>;
}

async function versioned<T>(path: string, signal: AbortSignal): Promise<{ data: T; etag: string }> {
  const response = await request(path, { signal });
  const etag = response.headers.get("ETag");
  if (!etag) throw new Error("The server did not supply a revision for this record.");
  return { data: await response.json() as T, etag };
}

function query(fields: Record<string, string | number | undefined | null>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(fields)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
}

const eventPath = (id: Id) => `/events/${encodeURIComponent(id)}`;
const orderPath = (id: string) => `/orders/${encodeURIComponent(id)}`;
const userPath = (id: Id) => `/admin/users/${encodeURIComponent(id)}`;
const jobPath = (id: string) => `/seat-imports/${encodeURIComponent(id)}`;

async function* stream(
  path: string, signal: AbortSignal, after?: number, onComment?: () => void,
): AsyncGenerator<Frame> {
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (after !== undefined) headers["Last-Event-ID"] = String(after);
  const response = await request(path, { headers, signal, timeoutMs: null });
  if (!response.body || !response.headers.get("Content-Type")?.includes("text/event-stream")) {
    throw new Error("The server did not return an event stream.");
  }
  yield* readFrames(response.body, onComment);
}

const parse = <T>(frame: Frame): T => JSON.parse(frame.data) as T;

export async function* seatStream(
  id: Id, after: number, signal: AbortSignal, onComment?: () => void,
): AsyncGenerator<SeatFrame> {
  for await (const frame of stream(eventPath(id) + "/seat-stream", signal, after, onComment)) {
    switch (frame.event) {
      case "delta": yield { event: "delta", data: parse<Delta>(frame) }; break;
      case "layout": yield { event: "layout", data: parse<{ layout_ver: number }>(frame) }; break;
      case "progress": {
        const data = parse<{ head?: number; assigned_head?: number; oldest_unassigned_age_ms: number }>(frame);
        yield { event: "progress", data: { ...data, head: data.head ?? data.assigned_head ?? 0 } };
        break;
      }
      case "resync": yield { event: "resync" }; break;
      default: yield { event: "heartbeat" };
    }
  }
}

async function* monitoringStream<T>(path: string, events: string[], signal: AbortSignal): AsyncGenerator<T> {
  for await (const frame of stream(path, signal)) {
    if (events.includes(frame.event) || frame.event === "message") yield parse<T>(frame);
  }
}

type Collection<T> = T[] | { items: T[]; next_cursor?: string | null };
function page<T>(value: Collection<T>): Page<T> {
  return Array.isArray(value) ? { items: value, next_cursor: null } : {
    items: value.items, next_cursor: value.next_cursor ?? null,
  };
}

function orderFrom(payload: OrderPayload): Order {
  return "id" in payload ? payload : { ...payload, id: payload.public_id };
}

function importFrom(payload: ImportPayload): ImportJob {
  return { ...payload, progress: payload.progress_percent === undefined ? payload.progress : payload.progress_percent / 100 };
}

function metricFrom(frame: LiveMetric): Metric {
  const reading = (source: MetricReading | null | undefined) => source && { ...source, rps: source.rps ?? source.req_total };
  const { edge, app, business } = frame;
  const totals = Array.isArray(business) ? business.reduce<Required<NonNullable<Metric["business"]>>>((sum, item) => ({
    hold_ok: sum.hold_ok + (item.hold_ok ?? 0),
    hold_conflict: sum.hold_conflict + (item.hold_conflict ?? 0),
    hold_soldout: sum.hold_soldout + (item.hold_soldout ?? 0),
    pay_ok: sum.pay_ok + (item.pay_ok ?? 0),
  }), { hold_ok: 0, hold_conflict: 0, hold_soldout: 0, pay_ok: 0 }) : business;
  return {
    ts: frame.ts, edge: reading(edge), app: reading(app), business: totals,
    connections: frame.connections ?? { active: edge?.conns_active, sse: app?.conns_sse },
    completeness: frame.completeness ?? (edge && app ? app.completeness ?? Number(frame.complete === true) : 0),
  };
}

function historyFrom(rows: HistoryMetric[]): Metric[] {
  const frames = new Map<Metric["ts"], LiveMetric>();
  for (const row of rows) {
    if ("source" in row) {
      const frame = frames.get(row.ts) ?? { ts: row.ts };
      frame[row.source] = row;
      frames.set(row.ts, frame);
    } else frames.set(row.ts, row);
  }
  return Array.from(frames.values(), metricFrom);
}

async function* metricsStream(signal: AbortSignal): AsyncGenerator<Metric> {
  for await (const frame of monitoringStream<LiveMetric>("/admin/metrics/stream", ["metric", "metrics"], signal)) {
    yield metricFrom(frame);
  }
}

async function* alertsStream(signal: AbortSignal): AsyncGenerator<AlertUpdate> {
  for await (const frame of monitoringStream<Collection<Alert> | Alert>("/admin/alerts/stream", ["alert", "alerts"], signal)) {
    yield "id" in frame ? { alert: frame } : { snapshot: page(frame).items };
  }
}

export const api = {
  register: (email: string, password: string, signal: AbortSignal) => json<User>("/auth/register", {
    method: "POST", body: { email, password }, signal, authOptional: true,
  }),
  async login(email: string, password: string, signal: AbortSignal): Promise<Session> {
    const session = await json<Session>("/auth/login", {
      method: "POST", body: { email, password }, signal, authOptional: true,
    });
    csrf = session.csrf_token;
    return session;
  },
  async me(signal: AbortSignal): Promise<Session> {
    const session = await json<Session>("/auth/me", { signal, authOptional: true });
    csrf = session.csrf_token;
    return session;
  },
  async logout(): Promise<void> {
    await request("/auth/logout", { method: "POST" });
    csrf = "";
  },
  events: (filters: { limit?: number; cursor?: string | null; category?: string; q?: string; sort?: string },
    signal: AbortSignal) => json<Page<EventListItem>>("/events" + query(filters), { signal }),
  event: (id: Id, signal: AbortSignal) => json<EventInfo>(eventPath(id), { signal }),
  eventRevision: (id: Id, signal: AbortSignal) => versioned<EventInfo>(eventPath(id), signal),
  zones: async (id: Id, signal: AbortSignal) => {
    const data = await json<Collection<Zone> | { zones: Zone[] }>(eventPath(id) + "/zones", { signal });
    return "zones" in data ? data.zones : page(data).items;
  },
  createEvent: (body: EventInput & { zones: Zone[] }, signal: AbortSignal) =>
    json<EventInfo>("/events", { method: "POST", body, signal }),
  updateEvent: (id: Id, body: Partial<EventInput>, etag: string, signal: AbortSignal) =>
    json<EventInfo>(eventPath(id), { method: "PATCH", body, signal, headers: { "If-Match": etag } }),
  publish: (id: Id, etag: string, signal: AbortSignal) =>
    request(eventPath(id) + "/publish", { method: "POST", signal, headers: { "If-Match": etag } }),
  deleteEvent: (id: Id, signal: AbortSignal) => request(eventPath(id), { method: "DELETE", signal }),
  async seats(id: Id, minLayout: number | undefined, signal: AbortSignal): Promise<Snapshot> {
    const data = await json<Snapshot & { last_assigned_seq?: number }>(
      eventPath(id) + "/seats" + query({ min_layout_ver: minLayout }), { signal },
    );
    return { ...data, last_seq: data.last_seq ?? data.last_assigned_seq ?? 0 };
  },
  book: async (id: Id, body: Booking, key: string, signal: AbortSignal) =>
    orderFrom(await json<OrderPayload>(eventPath(id) + "/orders", {
      method: "POST", body, signal, headers: { "Idempotency-Key": key },
    })),
  async orders(cursor: string | null, signal: AbortSignal): Promise<Page<Order>> {
    const { items, next_cursor } = page(await json<Collection<OrderPayload>>("/orders" + query({ cursor }), { signal }));
    return { items: items.map(orderFrom), next_cursor };
  },
  adminOrders: (filters: AdminOrderFilters, cursor: string | null, signal: AbortSignal) =>
    json<Page<AdminOrder>>("/admin/orders" + query({ ...filters, cursor }), { signal }),
  order: async (id: string, signal: AbortSignal) => orderFrom(await json<OrderPayload>(orderPath(id), { signal })),
  cancel: (id: string, key: string, signal: AbortSignal) =>
    request(orderPath(id) + "/cancel", { method: "POST", signal, headers: { "Idempotency-Key": key } }),
  pay: (id: string, key: string, signal: AbortSignal) =>
    json<Checkout>(orderPath(id) + "/payments", { method: "POST", signal, headers: { "Idempotency-Key": key } }),
  async upload(id: Id, file: File, mode: ImportMode, signal: AbortSignal) {
    const body = new FormData();
    body.set("file", file);
    body.set("mode", mode);
    return importFrom(await json<ImportPayload>(eventPath(id) + "/seat-imports", {
      method: "POST", body, signal, timeoutMs: 120_000,
    }));
  },
  importJob: async (id: string, signal: AbortSignal) => importFrom(await json<ImportPayload>(jobPath(id), { signal })),
  reportUrl: (id: Id) => prefix + eventPath(id) + "/reports/sales.xlsx" + query({ tz: "Asia/Taipei" }),
  importErrors: (id: string, signal: AbortSignal) => request(jobPath(id) + "/errors.csv", { signal }),
  metrics: async (from: string, to: string, signal: AbortSignal) => {
    const data = await json<Collection<HistoryMetric> | { frames: LiveMetric[] }>(
      "/admin/metrics" + query({ from, to }), { signal },
    );
    return historyFrom("frames" in data ? data.frames : page(data).items);
  },
  metricsStream,
  alerts: async (signal: AbortSignal) =>
    page(await json<Collection<Alert>>("/admin/alerts?state=firing", { signal })).items,
  alertsStream,
  ack: (id: Id, signal: AbortSignal) =>
    request(`/admin/alerts/${encodeURIComponent(id)}/ack`, { method: "POST", signal }),
  recentLogs: async (filters: RecentLogFilters, signal: AbortSignal) =>
    (await json<{ items: RecentLog[] }>("/admin/logs/recent" + query({ ...filters }), { signal })).items,
  trace: async (id: string, signal: AbortSignal) => {
    const data = await json<Collection<TraceEntry> | { entries: TraceEntry[] } | RequestLogs>(
      `/admin/requests/${encodeURIComponent(id)}/logs`, { signal },
    );
    if (!Array.isArray(data) && "entries" in data) return data.entries;
    if ("service_logs" in data) {
      const entries = data.access_log ? [{ ...data.access_log, service: "nginx" }, ...data.service_logs] : data.service_logs;
      return entries.sort((a, b) => Date.parse(a.ts ?? "") - Date.parse(b.ts ?? ""));
    }
    return page(data).items;
  },
  users: (cursor: string | null, signal: AbortSignal) =>
    json<Page<AdminUser>>("/admin/users" + query({ cursor }), { signal }),
  user: (id: Id, signal: AbortSignal) => versioned<AdminUser>(userPath(id), signal),
  updateUser: (id: Id, body: Partial<Pick<AdminUser, "role" | "is_active">>, etag: string, signal: AbortSignal) =>
    json<AdminUser>(userPath(id), { method: "PATCH", body, signal, headers: { "If-Match": etag } }),
};
