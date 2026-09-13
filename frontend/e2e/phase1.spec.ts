import {expect, test, type Page} from "@playwright/test";

function requiredSecret(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

async function signIn(page: Page, username: string, password: string) {
  await page.goto("/login");
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Password").fill(password);
  const login = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/api/v1/auth/login"
    && response.request().method() === "POST",
  );
  await page.getByRole("button", {name: "Sign in"}).click();
  expect((await login).status(), "Credential endpoint status").toBe(200);
  await expect(page).toHaveURL(/\/roots$/);
}

test.afterEach(async ({page}, testInfo) => {
  // Playwright generates an accessibility snapshot even with traces disabled.
  // Clear fields before its teardown so failure context cannot retain secrets.
  if (!page.isClosed()) await page.locator("input, textarea").evaluateAll((fields) => {
    for (const field of fields) (field as HTMLInputElement).value = "";
  });
  if (testInfo.status === testInfo.expectedStatus || page.isClosed()) return;
  await page.screenshot({
    path: testInfo.outputPath("sanitized.png"),
    mask: [page.locator("input, textarea")],
    fullPage: false,
  });
});

test("Alice isolation, refresh, phone accessibility, and revoked restoration", async ({page}) => {
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  await expect(page.getByRole("heading", {name: "Alice files"})).toBeVisible();
  await expect(page.getByText("Bob files")).toHaveCount(0);
  await expect(page.getByText("Original access: read only")).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", {name: "Alice files"})).toBeVisible();

  const rootButton = page.getByRole("button", {name: /Alice files/});
  await rootButton.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("File browsing arrives in Phase 2.")).toBeVisible();
  await expect(rootButton).toHaveAttribute("aria-expanded", "true");

  for (const width of [320, 390]) {
    await page.setViewportSize({width, height: 844});
    for (const control of await page.locator("button, a").all()) {
      const box = await control.boundingBox();
      expect(box?.width).toBeGreaterThanOrEqual(44);
      expect(box?.height).toBeGreaterThanOrEqual(44);
    }
    expect(await page.evaluate(() =>
      document.documentElement.scrollWidth <= window.innerWidth,
    )).toBe(true);
  }
  await page.emulateMedia({reducedMotion: "reduce"});
  const duration = await page.locator(".root-card__chevron").evaluate((element) =>
    parseFloat(getComputedStyle(element).transitionDuration),
  );
  expect(duration).toBeLessThanOrEqual(0.00001);
  await expect(page.locator("html")).toHaveCSS("color-scheme", "dark");

  // Keep the real authorized response shape while exercising long display names.
  await page.route("**/api/v1/roots", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.roots[0].displayName = "Archive " + "long-unbroken-name".repeat(8);
    await route.fulfill({response, json: payload});
  });
  await page.setViewportSize({width: 320, height: 844});
  await page.reload();
  await expect(page.getByRole("heading", {name: /^Archive /})).toBeVisible();
  expect(await page.evaluate(() =>
    document.documentElement.scrollWidth <= window.innerWidth,
  )).toBe(true);
  await page.unroute("**/api/v1/roots");

  // Revoke via the same browser cookie jar, then simulate a restored document.
  const csrf = await (await page.request.get("/api/v1/auth/csrf")).json();
  const logout = await page.request.post("/api/v1/auth/logout", {
    headers: {"X-CSRFToken": csrf.csrfToken},
  });
  expect(logout.status()).toBe(204);
  await page.evaluate(() =>
    window.dispatchEvent(new PageTransitionEvent("pageshow", {persisted: true})),
  );
  await expect(page.getByRole("heading", {name: "Sign in"})).toBeVisible();
  await expect(page.getByText("Alice files")).toHaveCount(0);
  await expect(page.getByRole("heading", {name: /^Archive /})).toHaveCount(0);
});

test("Bob receives only group access and logout stays private across history", async ({page}) => {
  await signIn(page, "bob", requiredSecret("E2E_BOB_PASSWORD"));
  await expect(page.getByRole("heading", {name: "Bob files"})).toBeVisible();
  await expect(page.getByText("Alice files")).toHaveCount(0);
  await page.evaluate(() => history.pushState({}, "", "/roots?return=1"));
  await page.getByRole("button", {name: "Sign out"}).click();
  await expect(page.getByRole("heading", {name: "Sign in"})).toBeVisible();
  await expect(page.getByRole("button", {name: "Sign in"})).toBeEnabled();
  await page.goBack();
  await expect(page.getByRole("heading", {name: "Sign in"})).toBeVisible();
  await expect(page.getByText("Bob files")).toHaveCount(0);
  await page.goForward();
  await expect(page.getByText("Bob files")).toHaveCount(0);
  expect((await page.request.get("/api/v1/auth/session")).status()).toBe(401);
});

test("a platform superuser has operational access but no implicit product grants", async ({page}) => {
  await signIn(page, "phase1-admin", requiredSecret("E2E_ADMIN_PASSWORD"));
  await expect(page.getByRole("heading", {name: "No roots assigned"})).toBeVisible();
  await expect(page.getByText("Alice files")).toHaveCount(0);
  await expect(page.getByText("Bob files")).toHaveCount(0);
  const status = await page.request.get("/api/v1/admin/operations/status");
  expect(status.status()).toBe(200);
  expect(await status.text()).not.toContain("/srv/aegis");
});

test("anonymous boundaries and bad credentials disclose no private metadata", async ({page, request}) => {
  for (const path of ["/__aegis_roots/e2e-alice/private.jpg", "/__aegis_derivatives/private.jpg"]) {
    const response = await request.get(path);
    expect(response.status()).toBe(404);
    expect(await response.text()).not.toContain("/srv/aegis");
  }
  const response = await request.get("/api/v1/roots");
  expect(response.status()).toBe(401);
  expect(response.headers()["cache-control"]).toContain("no-store");
  const body = await response.text();
  expect(body).not.toMatch(/Alice files|e2e-alice|\/srv\/aegis/);
  await page.goto("/login");
  await page.getByLabel("Username").fill("alice");
  await page.getByLabel("Password").fill("definitely-not-the-password");
  await page.getByRole("button", {name: "Sign in"}).click();
  await expect(page.getByRole("alert")).toHaveText(
    "The username or password was not accepted.",
  );
  await expect(page).toHaveURL(/\/login$/);
});
