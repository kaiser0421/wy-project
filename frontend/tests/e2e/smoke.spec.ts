import { expect, test } from "@playwright/test";

test("the seat map follows the system and saved theme choices", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/?mock=1");
  const theme = page.getByRole("combobox", { name: "Theme" });
  const map = page.locator(".map-viewport canvas");
  const background = () => map.evaluate((canvas: HTMLCanvasElement) => {
    const pixel = canvas.getContext("2d")!.getImageData(0, 0, 1, 1).data;
    return Array.from(pixel);
  });

  await expect(theme).toHaveValue("system");
  await expect.poll(background).toEqual([242, 246, 249, 255]);
  await page.emulateMedia({ colorScheme: "dark" });
  await expect.poll(background).toEqual([18, 28, 42, 255]);

  await theme.selectOption("light");
  await expect.poll(background).toEqual([242, 246, 249, 255]);
  await page.reload();
  await expect(theme).toHaveValue("light");
  await expect.poll(background).toEqual([242, 246, 249, 255]);

  await theme.selectOption("dark");
  await page.emulateMedia({ colorScheme: "light" });
  await expect.poll(background).toEqual([18, 28, 42, 255]);
  await theme.selectOption("system");
  await expect.poll(background).toEqual([242, 246, 249, 255]);
});

test("buyer can choose a seat by keyboard, create a hold, and see payment settle", async ({ page }) => {
  await page.goto("/?mock=1");
  await expect(page.getByRole("heading", { name: "Harbor Lights Festival" })).toBeVisible();
  await expect(page.getByText("10,000 seats across 4 zones")).toBeVisible();
  const map = page.getByLabel("Interactive seat map");
  await map.focus();
  await map.press("ArrowRight");
  await expect(page.locator(".seat-readout")).toContainText("seat 2 · available");
  await map.press("Enter");
  await expect(page.getByText("1 of 4 seats selected")).toBeVisible();
  await page.getByRole("button", { name: "Hold selected seats" }).click();
  await expect(page.getByText("Pending payment", { exact: true })).toBeVisible();
  await expect(page.getByText(/Hold expires in/)).toBeVisible();
  await page.getByRole("button", { name: "Open checkout" }).click();
  await expect(page.getByText("Paid", { exact: true })).toBeVisible({ timeout: 8000 });
});

test("organizer creates a draft, uploads seats, and publishes from the current revision", async ({ page }) => {
  await page.goto("/?mock=1#/login");
  await page.getByRole("button", { name: "User", exact: true }).click();
  await page.getByRole("link", { name: "Events", exact: true }).click();
  // Organizers reach the create form from the Events list, where they look first.
  await page.getByRole("link", { name: "Create an event" }).click();
  await page.getByLabel("Event title").fill("Community concert");
  await page.getByLabel("Category").fill("Music");
  await page.getByLabel("Venue").fill("Community hall");
  await page.getByLabel("Sales start (local time)").fill("2026-06-01T10:00");
  await page.getByLabel("Sales end (local time)").fill("2026-06-01T11:00");
  await page.getByLabel("Zone code").fill("A");
  await page.getByLabel("Zone name").fill("Stalls");
  await page.getByLabel("Price in TWD").fill("750");
  await page.getByRole("button", { name: "Create draft" }).click();
  await expect(page.getByRole("heading", { name: "Community concert" })).toBeVisible();
  await page.getByLabel("CSV or JSON file").setInputFiles({
    name: "seats.csv", mimeType: "text/csv", buffer: Buffer.from("zone_code,row_label,seat_number\nA,1,1\n"),
  });
  await page.getByRole("button", { name: "Upload seats" }).click();
  await expect(page.getByText("Succeeded", { exact: true })).toBeVisible({ timeout: 8000 });
  await page.getByRole("button", { name: "Publish event" }).click();
  await expect(page.getByText("Published", { exact: true })).toBeVisible();
});

test("viewer can browse and the seat page fits a 400px viewport", async ({ page }) => {
  await page.setViewportSize({ width: 400, height: 800 });
  await page.goto("/?mock=1#/login");
  await page.getByRole("button", { name: "Viewer", exact: true }).click();
  await expect(page.getByRole("link", { name: "Harbor Lights Festival" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Create an event" })).toHaveCount(0);
  await page.getByRole("link", { name: "Harbor Lights Festival" }).click();
  await expect(page.getByLabel("Interactive seat map")).toBeVisible();
  await expect(page.getByRole("button", { name: "Hold selected seats" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Organizer" })).toHaveCount(0);
  expect(await page.locator("body").evaluate(body => body.scrollWidth)).toBeLessThanOrEqual(400);
  await page.goto("/?mock=1#/admin");
  await expect(page.getByRole("heading", { name: "Access restricted" })).toBeVisible();
});

test("admin can view graphs, acknowledge an alert, look up a trace, and edit access", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/?mock=1#/login");
  await page.getByRole("button", { name: "Admin", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Monitoring" })).toBeVisible();
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  await expect(page.locator(".uplot canvas")).toHaveCount(4);
  await page.getByRole("combobox", { name: "Theme" }).selectOption("dark");
  await expect.poll(() => page.locator(".uplot canvas").first().evaluate((canvas: HTMLCanvasElement) => {
    const pixels = canvas.getContext("2d")!.getImageData(0, 0, canvas.width, canvas.height).data;
    // Incomplete-data shading uses the dark --chart-incomplete token (#26374c).
    return pixels.some((red, index) => index % 4 === 0 && red === 38 &&
      pixels[index + 1] === 55 && pixels[index + 2] === 76 && pixels[index + 3] === 255);
  })).toBe(true);
  await page.getByRole("button", { name: "Acknowledge" }).click();
  await expect(page.getByText("Acknowledged", { exact: true })).toBeVisible();
  await page.getByPlaceholder("Paste a request ID").fill("demo-request");
  await page.getByRole("button", { name: "Find request" }).click();
  await expect(page.getByText("request_finished")).toBeVisible();
  await page.getByRole("link", { name: "Manage users" }).click();
  await page.getByRole("button", { name: "Edit user@demo.test" }).click();
  await page.getByLabel("Role", { exact: true }).selectOption("viewer");
  await page.getByRole("button", { name: "Save account" }).click();
  await expect(page.getByRole("row", { name: /user@demo.test Viewer/ })).toBeVisible();
  expect(pageErrors).toEqual([]);
});
