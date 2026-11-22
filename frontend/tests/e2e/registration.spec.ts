import { expect, test } from "@playwright/test";

test("registration shows field errors, respects rate limits, and returns to sign in", async ({ page }) => {
  const now = Date.now();
  await page.clock.setFixedTime(now);
  let attempts = 0;
  await page.route("**/api/v1/auth/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/me")) {
      await route.fulfill({ status: 401 });
      return;
    }
    if (path.endsWith("/login")) {
      await route.fulfill({ json: { user: { id: 7, email: "buyer@example.test", role: "user" }, csrf_token: "token" } });
      return;
    }
    expect(path).toBe("/api/v1/auth/register");
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({ email: "buyer@example.test", password: "Secret-example" });
    attempts++;
    if (attempts === 1) {
      await route.fulfill({ status: 422, contentType: "application/problem+json", json: {
        code: "validation-failed", errors: [{ loc: ["body", "password"], msg: "Choose a stronger password." }],
      } });
    } else if (attempts === 2) {
      await route.fulfill({ status: 429, headers: { "Retry-After": "2" },
        contentType: "application/problem+json", json: { code: "rate-limited", retryable: true } });
    } else {
      await route.fulfill({ status: 201, json: { id: 7, email: "buyer@example.test", role: "user" } });
    }
  });
  await page.route("**/api/v1/events/**", route => route.fulfill({ status: 404 }));
  await page.goto("/#/events/17");
  await page.getByRole("link", { name: "Create an account" }).click();
  // The sign-in form has fields with the same labels; wait until the registration form has replaced it.
  await expect(page).toHaveURL(/#\/register$/);
  await expect(page.getByRole("heading", { name: "Create an account" })).toBeVisible();
  const email = page.getByLabel("Email address");
  const password = page.getByLabel("Password");
  await email.fill("buyer@example.test");
  await password.fill("Secret-example");
  const create = page.getByRole("button", { name: "Create account", exact: true });
  await create.click();
  await expect(password).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByText("Choose a stronger password.")).toBeVisible();
  await create.click();
  await expect(page.getByText(/Too many registration attempts/)).toBeVisible();
  await expect(page.getByRole("button", { name: /Create account \(\d+s\)/ })).toBeDisabled();
  expect(attempts).toBe(2);
  await page.clock.setFixedTime(now + 2000);
  await create.click();
  await expect(page.getByText(/Account created/)).toBeVisible();
  await expect(email).toHaveValue("buyer@example.test");
  await page.getByLabel("Password").fill("Secret-example");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/#\/events\/17$/);
});
