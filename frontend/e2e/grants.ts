import {expect, type Page} from "@playwright/test";
import {requiredSecret, submitAdminLogin} from "./auth";

/**
 * Sets a synthetic root's only grant (Alice's user grant, or Bob's group grant) through the real
 * Django admin form as the synthetic superuser, in a separate browser context with its own cookie
 * jar. `permissions` is the grant's decimal permission bits.
 */
export async function setRootGrant(page: Page, rootId: string, permissions: string) {
  if (!/^[0-9]{1,3}$/.test(permissions)) throw new Error("Invalid synthetic permissions");
  const browser = page.context().browser();
  if (!browser) throw new Error("Browser is unavailable");
  const context = await browser.newContext({baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:18080"});
  try {
    const admin = await context.newPage();
    const signedIn = await submitAdminLogin(
      admin, "phase1-admin", requiredSecret("E2E_ADMIN_PASSWORD"), "/admin/roots/rootgrant/",
    );
    expect(signedIn.status()).toBe(302);
    await expect(admin).toHaveURL(/\/admin\/roots\/rootgrant\/$/);
    // Grants list their root by opaque ID; the seed gives each synthetic root exactly one grant.
    const rows = admin.locator("#result_list tbody tr").filter({hasText: rootId});
    await expect(rows).toHaveCount(1);
    await rows.locator('a[href$="/change/"]').first().click();
    await expect(admin).toHaveURL(/\/admin\/roots\/rootgrant\/[0-9a-f-]{36}\/change\/$/);
    await admin.locator("#id_permissions").fill(permissions);
    const saved = admin.waitForResponse((response) => response.request().method() === "POST"
      && /^\/admin\/roots\/rootgrant\/[0-9a-f-]{36}\/change\/$/.test(new URL(response.url()).pathname));
    await admin.getByRole("button", {name: "Save", exact: true}).click();
    expect((await saved).status()).toBe(302);
    await expect(admin).toHaveURL(/\/admin\/roots\/rootgrant\/$/);
  } finally {
    await context.close();
  }
}
