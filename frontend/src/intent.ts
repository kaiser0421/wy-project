import { ApiError } from "./api";

export function idempotencyKey(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
}

/** A timeout may hide a committed mutation, so an uncertain outcome keeps its key and body. */
export function retainIntent(error: unknown): boolean {
  return !(error instanceof ApiError) || error.retryable || error.status >= 500
    || error.status === 408 || error.status === 429 || error.code === "idempotency-in-progress";
}
