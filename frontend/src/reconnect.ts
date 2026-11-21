export class SeatReconnect {
  private failures = 0;
  private caughtUpSince?: number;

  caughtUp(now: number): void {
    this.caughtUpSince ??= now;
    // A short-lived stream can report one good frame and still cause a reconnect loop.
    if (now - this.caughtUpSince >= 10_000) this.failures = 0;
  }

  lagging(): void {
    this.caughtUpSince = undefined;
  }

  delay(jitter = Math.random()): number {
    this.lagging();
    const base = Math.min(6000, 400 * 2 ** this.failures);
    this.failures = Math.min(4, this.failures + 1);
    return Math.min(6000, base + 200 * jitter);
  }
}
