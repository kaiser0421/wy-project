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
    const { edge, app, connections, ...frame } = metric;
    const edgeSecond = edge?.ts === undefined ? second : metricSecond(edge.ts);
    const appSecond = app?.ts === undefined ? second : metricSecond(app.ts);
    // Sales remain on the frame's second. HTTP and connection gauges follow their source.
    if (edgeSecond === second && appSecond === second) {
      this.merge(second, metric, replace);
      return;
    }
    this.merge(second, { ...frame, completeness: undefined }, replace);
    if (edge && Number.isFinite(edgeSecond)) {
      this.merge(edgeSecond, { ts: edgeSecond, edge, connections: { active: connections?.active } }, replace);
    }
    if (app && Number.isFinite(appSecond)) {
      this.merge(appSecond, { ts: appSecond, app, connections: { sse: connections?.sse },
        completeness: app.completeness ?? frame.completeness }, replace);
    }
  }

  private merge(second: number, metric: Metric, replace: boolean): void {
    const current = this.samples.get(second);
    const preferred = replace || (metric.completeness ?? 0) > (current?.completeness ?? 0);
    const pick = <T>(previous: T | undefined, incoming: T | undefined) =>
      preferred ? incoming ?? previous : previous ?? incoming;
    this.samples.set(second, {
      ts: metric.ts,
      edge: pick(current?.edge, metric.edge),
      app: pick(current?.app, metric.app),
      business: pick(current?.business, metric.business),
      completeness: pick(current?.completeness, metric.completeness),
      connections: {
        active: pick(current?.connections?.active, metric.connections?.active),
        sse: pick(current?.connections?.sse, metric.connections?.sse),
      },
    });
  }

  window(last: number): { seconds: number[]; samples: Sample[] } {
    const first = last - WINDOW_SECONDS + 1;
    for (const second of this.samples.keys()) if (second < first) this.samples.delete(second);
    const seconds = Array.from({ length: WINDOW_SECONDS }, (_, index) => first + index);
    return { seconds, samples: seconds.map(second => this.samples.get(second) ?? null) };
  }
}

export const incomplete = (sample: Sample): boolean => sample?.completeness == null || sample.completeness < 1;
