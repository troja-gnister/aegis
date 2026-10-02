import {expect, type Page} from "@playwright/test";

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
