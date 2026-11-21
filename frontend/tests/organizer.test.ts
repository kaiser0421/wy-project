// @vitest-environment jsdom
import { expect, it, vi } from "vitest";
import { api, type ImportJob, type User } from "../src/api";
import { managePage } from "../src/organizer";

it("shows the import stage as a recovered job advances through validation and writing", async () => {
  vi.useFakeTimers();
  const navigation = new AbortController();
  const root = document.createElement("main");
  const user: User = { id: 1, email: "owner@example.com", role: "user" };
  const storage = "ticketing:import:1:2";
  sessionStorage.setItem(storage, "job");
  const job: ImportJob = {
    id: "job", status: "running", processed_rows: 250, total_rows: null,
    valid_rows: null, error_rows: null, progress: 0.01,
  };
  vi.spyOn(api, "eventRevision").mockResolvedValue({
    data: {
      id: 2, owner_id: 1, status: "draft", title: "Concert", description: "",
      venue: "Hall", category: "music", sales_start_at: "2026-10-01T00:00:00Z",
      sales_end_at: "2026-11-01T00:00:00Z", zones: [],
    },
    etag: '"1"',
  });
  vi.spyOn(api, "importJob")
    .mockResolvedValueOnce(job)
    .mockResolvedValueOnce({ ...job, total_rows: 250, progress: 0.98 })
    .mockResolvedValueOnce({ ...job, total_rows: 250, progress: 0.99 });
  try {
    await managePage(root, 2, user, navigation.signal);
    await vi.advanceTimersByTimeAsync(0);
    const progress = root.querySelector("progress")!;
    const status = progress.closest("section")!.querySelector('[role="status"]')!;
    expect(status.textContent).toBe("Parsing seat file…");
    expect(progress.value).toBe(1);
    await vi.advanceTimersByTimeAsync(650);
    expect(status.textContent).toBe("Validating seats…");
    expect(progress.value).toBe(98);
    await vi.advanceTimersByTimeAsync(650);
    expect(status.textContent).toBe("Writing seats…");
    expect(progress.value).toBe(99);
  } finally {
    navigation.abort();
    sessionStorage.removeItem(storage);
    vi.restoreAllMocks();
    vi.useRealTimers();
  }
});
