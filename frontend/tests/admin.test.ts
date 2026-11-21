// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { adminPage } from "../src/admin";
import { api, ApiError, type RecentLog } from "../src/api";

vi.mock("uplot", () => ({ default: vi.fn() }));

const warning: RecentLog = {
  ts: "2026-11-20T12:00:00Z", service: "api", level: "warning", event: "request.db_overload",
  request_id: "request-1", attrs: { error_type: "TimeoutError" },
};
const error: RecentLog = {
  ...warning, service: "worker", level: "error", event: "worker.failed", request_id: null,
};

let navigation: AbortController;
let visibility: DocumentVisibilityState;
let root: HTMLElement;

beforeEach(() => {
  vi.useFakeTimers();
  navigation = new AbortController();
  visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
  vi.stubGlobal("requestAnimationFrame", vi.fn());
  vi.stubGlobal("matchMedia", () => new EventTarget());
  vi.stubGlobal("ResizeObserver", class {
    observe = vi.fn();
    disconnect = vi.fn();
  });
  vi.spyOn(api, "alerts").mockRejectedValue(new ApiError(403, "forbidden", "Forbidden"));
  root = document.createElement("main");
  document.body.append(root);
});

afterEach(() => {
  navigation.abort();
  root.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function setVisibility(state: DocumentVisibilityState): void {
  visibility = state;
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("recent warnings and errors", () => {
  it("renders a labelled log table and runs the request trace from a request ID", async () => {
    vi.spyOn(api, "recentLogs").mockResolvedValue([warning, error]);
    const trace = vi.spyOn(api, "trace").mockResolvedValue([{ ...warning, event: warning.event ?? undefined }]);
    adminPage(root, navigation.signal);
    await vi.advanceTimersByTimeAsync(0);

    const region = root.querySelector('[aria-label="Recent warnings and errors"]')!;
    expect([...region.querySelectorAll('th[scope="col"]')].map(cell => cell.textContent))
      .toEqual(["Time", "Service", "Level", "Event", "Request ID"]);
    const rows = region.querySelectorAll("tbody tr");
    expect(rows).toHaveLength(2);
    expect(rows[0]?.querySelector("time")?.dateTime).toBe(warning.ts);
    expect(rows[0]?.querySelector(".log-level.warning")?.textContent).toBe("warning");
    expect(rows[0]?.textContent).toContain(warning.event);
    expect(rows[1]?.querySelector(".log-level.error")?.textContent).toBe("error");
    expect(rows[1]?.querySelector("td:last-child")?.textContent).toBe("—");
    expect(rows[1]?.querySelector("button")).toBeNull();

    const request = rows[0]!.querySelector("button")!;
    expect(request.textContent).toBe("request-1");
    request.click();
    const input = root.querySelector<HTMLInputElement>('input[name="request_id"]')!;
    expect(input.value).toBe("request-1");
    expect(document.activeElement).toBe(input);
    expect(trace).toHaveBeenCalledWith("request-1", expect.any(AbortSignal));
  });

  it("refreshes on service changes and ignores superseded results", async () => {
    let completeFirst!: (items: RecentLog[]) => void;
    const first = new Promise<RecentLog[]>(resolve => { completeFirst = resolve; });
    const logs = vi.spyOn(api, "recentLogs").mockReturnValueOnce(first).mockResolvedValueOnce([error])
      .mockResolvedValueOnce([]);
    adminPage(root, navigation.signal);

    const filter = root.querySelector("select")!;
    expect(filter.labels?.[0]?.querySelector("span")?.textContent).toBe("Service");
    expect(filter.selectedOptions[0]?.textContent).toBe("All services");
    filter.value = "worker";
    filter.dispatchEvent(new Event("change"));
    expect(logs.mock.calls[0]?.[1].aborted).toBe(true);
    expect(logs.mock.calls[1]?.[0]).toEqual({ service: "worker" });
    completeFirst([warning]);
    await vi.advanceTimersByTimeAsync(0);
    const region = root.querySelector<HTMLElement>('[aria-label="Recent warnings and errors"]')!;
    expect(region.querySelector("tbody")?.textContent).toContain(error.event);
    expect(region.querySelector("tbody")?.textContent).not.toContain(warning.event);

    filter.value = "paymock";
    filter.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(0);
    expect(logs.mock.calls[2]?.[0]).toEqual({ service: "paymock" });
    expect(region.hidden).toBe(true);
    expect(region.parentElement?.querySelector('[role="status"]')?.textContent).toBe("No recent warnings or errors.");
  });

  it("polls every five seconds while visible and aborts on hide or navigation", async () => {
    const pending = new Promise<RecentLog[]>(() => {});
    const logs = vi.spyOn(api, "recentLogs").mockResolvedValueOnce([warning]).mockReturnValue(pending);
    setVisibility("hidden");
    adminPage(root, navigation.signal);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(logs).not.toHaveBeenCalled();

    setVisibility("visible");
    expect(logs).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(4_999);
    expect(logs).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(logs).toHaveBeenCalledTimes(2);
    setVisibility("hidden");
    expect(logs.mock.calls[1]?.[1].aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(logs).toHaveBeenCalledTimes(2);

    setVisibility("visible");
    expect(logs).toHaveBeenCalledTimes(3);
    navigation.abort();
    expect(logs.mock.calls[2]?.[1].aborted).toBe(true);
    setVisibility("visible");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(logs).toHaveBeenCalledTimes(3);
  });
});
