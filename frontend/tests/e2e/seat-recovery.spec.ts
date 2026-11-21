import { expect, test } from "@playwright/test";

test("gap recovery reconnects from the applied sequence and requests the required layout", async ({ page }) => {
  const cursor: (string | undefined)[] = [];
  const requestedLayouts: (string | null)[] = [];
  const seat = (version: number, status: string) => ({
    idx: 70, id: 188, zone: "A", row: "1", number: 1, version, status,
  });
  await page.route("**/api/v1/**", async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.endsWith("/auth/me")) {
      await route.fulfill({ json: { user: { id: 2, email: "test@example.test", role: "user" }, csrf_token: "test" } });
    } else if (path.endsWith("/events/1")) {
      await route.fulfill({ json: {
        id: 1, owner_id: 2, title: "Replay test", category: "Music", venue: "Hall", status: "published",
        sales_start_at: "2020-01-01T00:00:00Z", sales_end_at: "2099-01-01T00:00:00Z",
      } });
    } else if (path.endsWith("/seats")) {
      const required = url.searchParams.get("min_layout_ver");
      requestedLayouts.push(required);
      const layout = required ? 2 : 1;
      await route.fulfill({ json: {
        event_id: 1, layout_ver: layout, last_seq: required ? 13 : 10,
        zones: [{ code: "A", name: "Floor", price_twd: 500 }],
        seats: [required ? seat(5, "available") : seat(3, "held")],
      } });
    } else if (path.endsWith("/seat-stream")) {
      cursor.push(route.request().headers()["last-event-id"]);
      const frames = [
        'id: 13\nevent: delta\ndata: {"from":13,"to":13,"changes":[[70,"sold",4]]}\n\n',
        'id: 12\nevent: delta\ndata: {"from":11,"to":12,"changes":[[70,"sold",4]]}\n\n' +
          'event: layout\ndata: {"layout_ver":2}\n\n',
        'id: 14\nevent: delta\ndata: {"from":14,"to":14,"changes":[[70,"sold",4]]}\n\n',
      ];
      await route.fulfill({ contentType: "text/event-stream", body: frames[cursor.length - 1] ?? "" });
    } else {
      await route.fulfill({ status: 404 });
    }
  });
  await page.goto("/#/events/1");
  await expect.poll(() => cursor.slice(0, 4), { timeout: 15_000 }).toEqual(["10", "10", "13", "14"]);
  expect(requestedLayouts).toEqual([null, "2"]);
  await page.getByLabel("Interactive seat map").focus();
  await expect(page.locator(".seat-readout")).toContainText("available");
});

test("reload preserves the original booking and its retry countdown", async ({ page }) => {
  const now = Date.now();
  await page.clock.setFixedTime(now);
  const attempts: { body: unknown; key?: string; csrf?: string }[] = [];
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/me")) {
      await route.fulfill({ json: { user: { id: 2, email: "test@example.test", role: "user" }, csrf_token: "restored-token" } });
    } else if (path.endsWith("/events/1")) {
      await route.fulfill({ json: {
        id: 1, title: "Booking test", category: "Music", venue: "Hall", status: "published",
        sales_start_at: "2020-01-01T00:00:00Z", sales_end_at: "2099-01-01T00:00:00Z",
      } });
    } else if (path.endsWith("/seats")) {
      await route.fulfill({ json: {
        event_id: 1, layout_ver: 1, last_seq: 0, zones: [{ code: "A", name: "Floor", price_twd: 500 }], seats: [],
      } });
    } else if (path.endsWith("/seat-stream")) {
      await route.fulfill({ contentType: "text/event-stream", body: "" });
    } else if (path.endsWith("/orders")) {
      const headers = route.request().headers();
      attempts.push({ body: route.request().postDataJSON(), key: headers["idempotency-key"], csrf: headers["x-csrf-token"] });
      await route.fulfill({
        status: 409, contentType: "application/problem+json",
        headers: attempts.length === 1 ? { "Retry-After": "3" } : undefined,
        json: { code: attempts.length === 1 ? "idempotency-in-progress" : "insufficient-seats",
          retryable: attempts.length === 1 },
      });
    } else {
      await route.fulfill({ status: 404 });
    }
  });
  await page.goto("/#/events/1");
  await page.getByLabel("Quantity").selectOption("2");
  await page.getByRole("button", { name: "Find seats" }).click();
  await expect(page.getByRole("button", { name: /Retry last request \(\d+s\)/ })).toBeDisabled();
  await page.reload();
  await expect(page.getByText(/Pending confirmation/)).toBeVisible();
  await expect(page.getByRole("button", { name: /Retry last request/ })).toBeDisabled();
  expect(attempts).toHaveLength(1);
  await page.clock.setFixedTime(now + 3000);
  await page.getByRole("button", { name: "Retry last request" }).click();
  await expect(page.getByText(/too few seats/)).toBeVisible();
  expect(attempts).toHaveLength(2);
  expect(attempts[0]).toEqual({ body: { zone_code: "A", quantity: 2 }, key: expect.stringMatching(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  ), csrf: "restored-token" });
  expect(attempts[1]).toEqual(attempts[0]);
  await expect(page.getByText(/Pending confirmation/)).toBeHidden();
});

test("a lost payment response is recoverable after signing out and back in", async ({ page }) => {
  let csrf = "first-session";
  const attempts: { key?: string; csrf?: string; body: string | null }[] = [];
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/logout")) {
      csrf = "second-session";
      await route.fulfill({ status: 204 });
    } else if (path.endsWith("/auth/me") || path.endsWith("/auth/login")) {
      await route.fulfill({ json: { user: { id: 2, email: "test@example.test", role: "user" }, csrf_token: csrf } });
    } else if (path.endsWith("/orders/test-order/payments")) {
      const headers = route.request().headers();
      attempts.push({ key: headers["idempotency-key"], csrf: headers["x-csrf-token"], body: route.request().postData() });
      if (attempts.length === 1) await route.abort();
      else await route.fulfill({ json: {} });
    } else if (path.endsWith("/orders/test-order")) {
      await route.fulfill({ json: {
        id: "test-order", event_id: 9, event_title: "Payment retry",
        status: "pending_payment", expires_at: "2099-01-01T00:00:00Z",
        subtotal_twd: 100, fee_twd: 5, total_twd: 105,
      } });
    } else await route.fulfill({ json: { items: [], next_cursor: null } });
  });
  await page.goto("/#/orders/test-order");
  await page.getByRole("button", { name: "Open checkout" }).click();
  await expect(page.getByText(/server could not be reached/)).toBeVisible();
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByLabel("Email address").fill("test@example.test");
  await page.getByLabel("Password").fill("test-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("region", { name: "Pending confirmation" }).getByRole("link").click();
  await page.getByRole("button", { name: "Retry checkout" }).click();
  await expect(page.getByText(/Pending confirmation/)).toBeHidden();
  expect(attempts).toEqual([
    { key: expect.any(String), csrf: "first-session", body: null },
    { key: attempts[0]!.key, csrf: "second-session", body: null },
  ]);
});
