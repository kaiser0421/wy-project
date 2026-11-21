// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { adminOrdersPage } from "../src/admin-orders";
import { api, type AdminOrder, type User } from "../src/api";

vi.mock("uplot", () => ({ default: vi.fn() }));

const admin: User = { id: 10, email: "admin@example.test", role: "admin" };
const first: AdminOrder = {
  public_id: "order-1", event_id: 2, event_title: "Concert", buyer_email: "buyer@example.test",
  status: "pending_payment", quantity: 2, total_twd: 420, created_at: "2026-11-21T12:00:00Z",
  expires_at: "2026-11-21T12:10:00Z",
};
const second: AdminOrder = { ...first, public_id: "order-2", status: "paid" };
let root: HTMLElement;
let controller: AbortController;

beforeEach(() => {
  controller = new AbortController();
  root = document.createElement("main");
  document.body.append(root);
});

afterEach(() => {
  controller.abort();
  root.remove();
  localStorage.clear();
  vi.restoreAllMocks();
});

function control(text: string): HTMLButtonElement {
  return [...root.querySelectorAll("button")].find(button => button.textContent === text)!;
}

describe("All orders", () => {
  it("renders accessible summaries, applies filters and navigates using the next cursor", async () => {
    const list = vi.spyOn(api, "adminOrders")
      .mockResolvedValueOnce({ items: [first], next_cursor: "initial" })
      .mockResolvedValueOnce({ items: [first], next_cursor: "filtered" })
      .mockResolvedValueOnce({ items: [second], next_cursor: null })
      .mockResolvedValueOnce({ items: [first], next_cursor: "filtered" });
    adminOrdersPage(root, admin, controller.signal);
    await vi.waitFor(() => expect(root.querySelector("tbody tr")).toBeTruthy());

    expect(root.querySelector("h1")?.textContent).toBe("All orders");
    expect(root.querySelector('[role="region"]')?.getAttribute("aria-label")).toBe("Order results");
    expect(root.querySelectorAll('th[scope="col"]')).toHaveLength(9);
    expect(root.querySelector('th[scope="row"] a')?.getAttribute("href")).toBe("#/orders/order-1");
    expect(root.querySelector("tbody")?.textContent).toContain("Concert (#2)");
    expect(root.querySelector("tbody")?.textContent).toContain("buyer@example.test");
    expect([...root.querySelectorAll<HTMLTimeElement>("tbody time")].map(cell => cell.dateTime))
      .toEqual([first.created_at, first.expires_at]);

    const form = root.querySelector("form")!;
    const filters = {
      event_id: "2", status: "pending_payment", buyer_email: "buyer+vip",
      created_from: "2026-11-20T07:00", created_to: "2026-11-22T07:00",
    };
    for (const [name, value] of Object.entries(filters)) {
      const input = form.elements.namedItem(name) as HTMLInputElement | HTMLSelectElement;
      expect(input.labels).toHaveLength(1);
      input.value = value;
    }
    control("Apply filters").click();
    const expected = {
      ...filters, created_from: new Date(filters.created_from).toISOString(),
      created_to: new Date(filters.created_to).toISOString(),
    };
    await vi.waitFor(() => expect(control("Next page").disabled).toBe(false));
    expect(list).toHaveBeenNthCalledWith(2, expected, null, controller.signal);

    control("Next page").click();
    await vi.waitFor(() => expect(root.querySelector("tbody")?.textContent).toContain("order-2"));
    expect(list).toHaveBeenNthCalledWith(3, expected, "filtered", controller.signal);
    expect(document.activeElement?.textContent).toBe("Page 2");
    expect(control("Next page").disabled).toBe(true);

    control("Previous page").click();
    await vi.waitFor(() => expect(root.querySelector("tbody")?.textContent).toContain("order-1"));
    expect(list).toHaveBeenNthCalledWith(4, expected, null, controller.signal);
  });

  it("confirms cancellations and retries a failed request with the same idempotency key", async () => {
    vi.spyOn(api, "adminOrders").mockResolvedValue({ items: [first], next_cursor: null });
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const cancel = vi.spyOn(api, "cancel").mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(Response.json({ status: "cancelled" }));
    adminOrdersPage(root, admin, controller.signal);
    await vi.waitFor(() => expect(control("Cancel order")).toBeTruthy());

    control("Cancel order").click();
    expect(confirm).toHaveBeenCalledWith("Cancel order order-1 and release its held seats?");
    expect(cancel).not.toHaveBeenCalled();

    control("Cancel order").click();
    await vi.waitFor(() => expect(control("Retry cancellation")).toBeTruthy());
    control("Retry cancellation").click();
    await vi.waitFor(() => expect(root.querySelector("tbody")?.textContent).toContain("Cancelled"));
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(cancel).toHaveBeenCalledTimes(2);
    expect(cancel.mock.calls[0]).toEqual(["order-1", expect.any(String), controller.signal]);
    expect(cancel.mock.calls[1]).toEqual(cancel.mock.calls[0]);
    expect(control("Cancel order")).toBeUndefined();
    expect(document.activeElement?.getAttribute("href")).toBe("#/orders/order-1");
  });

  it("uses the same cancellation call for a paid order and displays its pending refund", async () => {
    vi.spyOn(api, "adminOrders").mockResolvedValue({ items: [second], next_cursor: null });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const cancel = vi.spyOn(api, "cancel").mockResolvedValue(Response.json({ status: "refund_pending" }));
    adminOrdersPage(root, admin, controller.signal);
    await vi.waitFor(() => expect(control("Request refund")).toBeTruthy());
    control("Request refund").click();
    await vi.waitFor(() => expect(root.querySelector("tbody")?.textContent).toContain("Refund pending"));
    expect(confirm).toHaveBeenCalledWith("Request a refund for order order-2?");
    expect(cancel).toHaveBeenCalledWith("order-2", expect.any(String), controller.signal);
  });
});

it("shows the All orders navigation and page only to Admins", async () => {
  const account: User = { id: 11, email: "buyer@example.test", role: "user" };
  vi.spyOn(api, "me").mockResolvedValue({ user: account, csrf_token: "csrf" });
  vi.spyOn(api, "logout").mockResolvedValue(undefined);
  vi.spyOn(api, "login").mockResolvedValue({ user: admin, csrf_token: "csrf" });
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
  const list = vi.spyOn(api, "adminOrders").mockResolvedValue({ items: [], next_cursor: null });
  const app = document.createElement("div");
  app.id = "app";
  document.body.append(app);
  const initialUrl = location.href;
  history.replaceState(null, "", "#/admin/orders");
  try {
    await import("../src/main");
    await vi.waitFor(() => expect(app.querySelector("h1")?.textContent).toBe("Access restricted"));
    const nav = app.querySelector('[aria-label="Main navigation"]')!;
    expect(nav.querySelector('a[href="#/orders"]')?.textContent).toBe("My orders");
    expect(nav.querySelector('a[href="#/admin/orders"]')).toBeNull();
    expect(list).not.toHaveBeenCalled();

    [...app.querySelectorAll("button")].find(button => button.textContent === "Sign out")!.click();
    await vi.waitFor(() => expect(app.querySelector("h1")?.textContent).toBe("Sign in"));
    app.querySelector<HTMLButtonElement>('button[type="submit"]')!.form!.dispatchEvent(new Event("submit"));
    await vi.waitFor(() => expect(app.querySelector('[aria-label="Main navigation"] a[href="#/admin/orders"]')
      ?.textContent).toBe("All orders"));
    history.replaceState(null, "", "#/admin/orders");
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await vi.waitFor(() => expect(app.querySelector("h1")?.textContent).toBe("All orders"));
    expect(list).toHaveBeenCalledOnce();
  } finally {
    history.replaceState(null, "", initialUrl);
    app.remove();
  }
});
