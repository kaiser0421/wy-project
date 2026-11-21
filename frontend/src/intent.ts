import { ApiError, mockMode, type Booking, type Id } from "./api";

const prefix = `ticketing:${mockMode ? "demo:" : ""}pending:`;
const replayWindowMs = 24 * 60 * 60 * 1000;

type IntentScope =
  | { operation: "booking"; event: Id }
  | { operation: "payment" | "cancellation"; event: Id; order: string };
type IntentBody = Booking | null;
export type PendingIntent<T extends IntentBody = IntentBody> = IntentScope & {
  key: string;
  body: T;
  timestamp: number;
  retryAt: number;
};

export function idempotencyKey(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
}

/** A timeout or an expired session can hide a committed mutation. */
export function retainIntent(error: unknown): boolean {
  return !(error instanceof ApiError) || error.retryable || error.status >= 500
    || [401, 408, 429].includes(error.status) || error.code === "idempotency-in-progress";
}

const accountPrefix = (account: Id) => `${prefix}${encodeURIComponent(account)}:`;

function readIntent(key: string): PendingIntent | undefined {
  const value = localStorage.getItem(key);
  if (!value) return;
  try {
    const record = JSON.parse(value) as PendingIntent | null;
    if (record && typeof record.key === "string" && Number.isFinite(record.timestamp)
      && Number.isFinite(record.retryAt) && ["booking", "payment", "cancellation"].includes(record.operation)) {
      return record;
    }
  } catch {
    // Invalid entries cannot be replayed.
  }
}

export function pendingIntents(account: Id): PendingIntent[] {
  try {
    return Object.keys(localStorage).filter(key => key.startsWith(accountPrefix(account)))
      .flatMap(key => readIntent(key) ?? []);
  } catch {
    return [];
  }
}

export function retryDelay(error: unknown, now = Date.now()): number {
  if (!(error instanceof ApiError) ||
      (![429, 503].includes(error.status) && error.code !== "idempotency-in-progress")) return 0;
  const value = error.retryAfter?.trim();
  if (value && /^\d+$/.test(value)) {
    const milliseconds = Number(value) * 1000;
    if (Number.isSafeInteger(milliseconds + now)) return milliseconds;
  }
  const date = value && /[a-z]/i.test(value) ? Date.parse(value) : NaN;
  return typeof date === "number" && Number.isFinite(date) ? Math.max(0, date - now) : 1000;
}

export function retrySeconds(deadline: number, now = Date.now()): number {
  return Math.max(0, Math.ceil((deadline - now) / 1000));
}

export function retryLabel(label: string, seconds: number): string {
  return seconds ? `${label} (${seconds}s)` : label;
}

export class IntentUnavailable extends Error {}

export class MutationIntent<T extends IntentBody> {
  private readonly storageKey: string;

  constructor(account: Id, private readonly scope: IntentScope) {
    const id = scope.operation === "booking" ? scope.event : scope.order;
    this.storageKey = `${accountPrefix(account)}${scope.operation}:${encodeURIComponent(id)}`;
  }

  get current(): PendingIntent<T> | undefined {
    try {
      return this.read();
    } catch {
      return undefined;
    }
  }

  get seconds(): number {
    return retrySeconds(this.current?.retryAt ?? 0);
  }

  get expired(): boolean {
    const current = this.current;
    return Boolean(current && Date.now() >= current.timestamp + replayWindowMs);
  }

  async dismissExpired(): Promise<void> {
    await this.lock(() => {
      const current = this.current;
      if (current && Date.now() >= current.timestamp + replayWindowMs) this.clear(current.key);
    });
  }

  async send<R>(body: T | undefined, request: (body: T, key: string) => Promise<R>): Promise<R> {
    const intent = await this.lock(() => this.prepare(body));
    try {
      const result = await request(intent.body, intent.key);
      await this.lock(() => this.clear(intent.key));
      return result;
    } catch (error) {
      const delay = retryDelay(error);
      if (!retainIntent(error)) await this.lock(() => this.clear(intent.key));
      else if (delay) await this.lock(() => this.defer(intent.key, Date.now() + delay));
      throw error;
    }
  }

  private async lock<R>(update: () => R): Promise<R> {
    // Same-origin tabs share the journal. Keep the lock only while updating storage.
    return typeof navigator !== "undefined" && navigator.locks
      ? navigator.locks.request(this.storageKey, update) : update();
  }

  private read(): PendingIntent<T> | undefined {
    return readIntent(this.storageKey) as PendingIntent<T> | undefined;
  }

  private prepare(body: T | undefined): PendingIntent<T> {
    let saved: PendingIntent<T> | undefined;
    try {
      saved = this.read();
      if (!saved && body !== undefined) {
        saved = { ...this.scope, body, key: idempotencyKey(), timestamp: Date.now(), retryAt: 0 };
        localStorage.setItem(this.storageKey, JSON.stringify(saved));
      }
    } catch {
      throw new IntentUnavailable("Allow browser storage before sending this request so it can be recovered.");
    }
    if (!saved) throw new IntentUnavailable("This request has already been resolved. Check My orders.");
    if (Date.now() >= saved.timestamp + replayWindowMs) {
      throw new IntentUnavailable("The retry window has ended. Check My orders for the result.");
    }
    if (retrySeconds(saved.retryAt)) throw new IntentUnavailable("Wait for the retry countdown.");
    return saved;
  }

  private clear(key: string): void {
    try {
      if (this.read()?.key === key) localStorage.removeItem(this.storageKey);
    } catch {
      // A storage failure after dispatch cannot change the server's answer.
    }
  }

  private defer(key: string, retryAt: number): void {
    try {
      const saved = this.read();
      if (saved?.key === key && saved.retryAt < retryAt) {
        localStorage.setItem(this.storageKey, JSON.stringify({ ...saved, retryAt }));
      }
    } catch {
      // Keep the original record if the retry deadline could not be saved.
    }
  }
}
