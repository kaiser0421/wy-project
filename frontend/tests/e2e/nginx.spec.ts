import { expect, test } from "@playwright/test";
import { randomUUID } from "node:crypto";

for (const role of ["user", "viewer", "admin"] as const) {
  test(`${role} can sign in and navigate the integrated stack through nginx`, async ({ page }) => {
    const email = process.env[`TICKETING_SMOKE_${role.toUpperCase()}_EMAIL`];
    const password = process.env[`TICKETING_SMOKE_${role.toUpperCase()}_PASSWORD`];
    const event = process.env.TICKETING_SMOKE_EVENT_ID;
    if (!email || !password || !event) throw new Error("Set the smoke credentials and published event ID.");
    await page.goto("/#/login");
    await page.getByLabel("Email address").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("heading", { name: "Events" })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("heading", { name: "Events" })).toBeVisible();

    if (role === "admin") {
      await page.getByRole("link", { name: "Monitoring" }).click();
      await expect(page.getByText("Live", { exact: true })).toBeVisible();
      await page.getByRole("link", { name: "Manage users" }).click();
      await expect(page.getByRole("row", { name: /\(you\)/ })).toBeVisible();
    } else {
      await page.goto(`/#/events/${encodeURIComponent(event)}`);
      await expect(page.getByLabel("Interactive seat map")).toBeVisible();
      await expect(page.getByText("Live", { exact: true })).toBeVisible();
      if (role === "viewer") {
        await expect(page.getByRole("link", { name: "Download sales report" })).toBeVisible();
        await expect(page.getByRole("link", { name: "My orders" })).toHaveCount(0);
      } else {
        await expect(page.getByRole("button", { name: "Hold selected seats" })).toBeVisible();
        await page.getByRole("link", { name: "Organizer" }).click();
        await expect(page.getByRole("heading", { name: "Create an event" })).toBeVisible();
      }
    }
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  });
}

test("buyer imports seats, publishes, and completes checkout through nginx", async ({ page }) => {
  test.skip(process.env.TICKETING_SMOKE_WRITE !== "1", "Requires a disposable integrated stack.");
  test.setTimeout(120_000);
  const email = process.env.TICKETING_SMOKE_USER_EMAIL;
  const password = process.env.TICKETING_SMOKE_USER_PASSWORD;
  if (!email || !password) throw new Error("Set the buyer's smoke credentials.");
  const title = `Smoke ${randomUUID()}`;
  const local = (ms: number) => {
    const date = new Date(ms);
    return new Date(ms - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 19);
  };
  const now = Date.now();
  await page.goto("/#/login");
  await page.getByLabel("Email address").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("link", { name: "Organizer", exact: true }).click();
  await page.getByLabel("Event title").fill(title);
  await page.getByLabel("Category").fill("Music");
  await page.getByLabel("Venue").fill("Smoke test venue");
  await page.getByLabel("Sales start (local time)").fill(local(now - 60_000));
  await page.getByLabel("Sales end (local time)").fill(local(now + 3_600_000));
  await page.getByLabel("Zone code").fill("A");
  await page.getByLabel("Zone name").fill("Floor");
  await page.getByLabel("Price in TWD").fill("1000");
  await page.getByRole("button", { name: "Create draft" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await page.getByLabel("CSV or JSON file").setInputFiles({
    name: "seats.csv", mimeType: "text/csv", buffer: Buffer.from("zone_code,row_label,seat_number\nA,1,1\n"),
  });
  await page.getByRole("button", { name: "Upload seats" }).click();
  await expect(page.getByText("Succeeded", { exact: true })).toBeVisible({ timeout: 60_000 });
  await page.getByRole("button", { name: "Publish event" }).click();
  await expect(page.getByText("Published", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "View seat map" }).click();
  const map = page.getByLabel("Interactive seat map");
  await map.focus();
  await map.press("Enter");
  await page.getByRole("button", { name: "Hold selected seats" }).click();
  await expect(page.getByText("Pending payment", { exact: true })).toBeVisible();
  const checkout = page.waitForEvent("popup");
  await page.getByRole("button", { name: "Open checkout" }).click();
  const provider = await checkout;
  await provider.getByRole("button", { name: "Pay", exact: true }).click();
  await expect(page.getByText("Paid", { exact: true })).toBeVisible({ timeout: 30_000 });
});
