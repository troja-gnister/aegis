import {expect, type Page, type Response} from "@playwright/test";

/** Synthetic credentials exist only in the runner environment, never in arguments or reports. */
export function requiredSecret(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

const LOGIN_ATTEMPTS = 8;
// The real gateway admits 10 sign-in requests per minute (burst 10) and answers
// 503 beyond that; one admission token returns every six seconds.
const LOGIN_RATE_WAIT_MS = 6_500;

/** Submits the real login form; waits out only the gateway rate limit. Returns the final status. */
export async function submitLogin(page: Page, username: string, password: string): Promise<number> {
  for (let attempt = 1; ; attempt += 1) {
    await page.getByLabel("Username").fill(username);
    await page.getByLabel("Password").fill(password);
    const login = page.waitForResponse((response) =>
      new URL(response.url()).pathname === "/api/v1/auth/login"
      && response.request().method() === "POST",
    );
    await page.getByRole("button", {name: "Sign in"}).click();
    const status = (await login).status();
    if (status !== 503 || attempt === LOGIN_ATTEMPTS) return status;
    await page.waitForTimeout(LOGIN_RATE_WAIT_MS);
  }
}

export async function signIn(page: Page, username: string, password: string) {
  await page.goto("/login");
  expect(await submitLogin(page, username, password), "Credential endpoint status").toBe(200);
  await expect(page).toHaveURL(/\/roots$/);
}

/**
 * Signs in through the real Django admin form behind the gateway, as a browser
 * navigation POST. Waits out only the gateway's admin login rate limit and
 * returns the final form response (a redirect on success).
 */
export async function submitAdminLogin(page: Page, username: string, password: string, next = "/admin/"): Promise<Response> {
  for (let attempt = 1; ; attempt += 1) {
    await page.goto(`/admin/login/?next=${encodeURIComponent(next)}`);
    await page.locator("#id_username").fill(username);
    await page.locator("#id_password").fill(password);
    const login = page.waitForResponse((response) =>
      new URL(response.url()).pathname === "/admin/login/"
      && response.request().method() === "POST",
    );
    await page.getByRole("button", {name: "Log in"}).click();
    const response = await login;
    if (response.status() !== 503 || attempt === LOGIN_ATTEMPTS) return response;
    await page.waitForTimeout(LOGIN_RATE_WAIT_MS);
  }
}
