import type { Metric } from "./api";

export const WINDOW_SECONDS = 300;
export type Sample = Metric | null;

export function metricSecond(value: Metric["ts"]): number {
  return Math.floor(typeof value === "string" ? Date.parse(value) / 1000
    : value >= 1e12 ? value / 1000 : value);
}

export class MetricWindow {
  private readonly samples = new Map<number, Metric>();

  add(metric: Metric, replace = true): void {
    const second = metricSecond(metric.ts);
    if (!Number.isFinite(second)) return;
    const current = this.samples.get(second);
    if (replace || !current) this.samples.set(second, metric);
    else if ((metric.completeness ?? 0) > (current.completeness ?? 0)) {
      // History can fill a late HTTP sample, while only the live frame carries sales activity.
      this.samples.set(second, { ...metric, business: metric.business ?? current.business });
    }
  }

  window(last: number): { seconds: number[]; samples: Sample[] } {
    const first = last - WINDOW_SECONDS + 1;
    for (const second of this.samples.keys()) if (second < first) this.samples.delete(second);
    const seconds = Array.from({ length: WINDOW_SECONDS }, (_, index) => first + index);
    return { seconds, samples: seconds.map(second => this.samples.get(second) ?? null) };
  }
}

export const incomplete = (sample: Sample): boolean => sample?.completeness == null || sample.completeness < 1;
