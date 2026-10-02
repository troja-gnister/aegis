import {expect, type Locator, type Page} from "@playwright/test";
import {requiredSecret, signIn, submitAdminLogin} from "./auth";
import {test} from "./safe-test";

// Real-stack journeys over the harness-owned synthetic source fixture. Every
// catalog response comes from the real indexer, API and gateway; tests only
// delay or alter outgoing requests, never fabricate a response body.

// Sign-in may wait for the gateway's real login rate limit shared by both suites.
test.describe.configure({timeout: 180_000});

const ENTRIES_PATH = /^\/api\/v1\/roots\/[0-9a-f-]{36}\/entries$/;

function isEntries(url: URL): boolean {
  return ENTRIES_PATH.test(url.pathname);
}

function entriesResponse(page: Page, check: (url: URL) => boolean = () => true) {
  return page.waitForResponse((response) => {
    const url = new URL(response.url());
    return isEntries(url) && response.request().method() === "GET" && check(url);
  });
}

function files(page: Page): Locator {
  return page.getByRole("list", {name: "Files"});
}

/** The visible empty-window notice; the polite live region repeats the same text. */
function emptyNotice(page: Page): Locator {
  return page.locator("p.files-page__empty");
}

function fileRow(page: Page, name: string): Locator {
  return page.getByRole("button", {name: `Select file ${name}`, exact: true});
}

function directoryRow(page: Page, name: string): Locator {
  return page.getByRole("button", {name: `Open directory ${name}`, exact: true});
}

async function openRoot(page: Page, name: string) {
  const firstPage = entriesResponse(page, (url) => !url.searchParams.has("cursor"));
  await page.getByRole("link", {name: new RegExp(name)}).click();
  expect((await firstPage).status()).toBe(200);
  await expect(page).toHaveURL(/\/files\/[0-9a-f-]{36}$/);
  await expect(files(page)).toBeVisible();
}

async function openFilters(page: Page): Promise<Locator> {
  await page.getByRole("button", {name: "Filters", exact: true}).click();
  const dialog = page.getByRole("dialog", {name: "Filter files"});
  await expect(dialog).toBeVisible();
  return dialog;
}

/** Applies the dialog and waits for the real first page; a changed filter never sends a cursor. */
async function applyFilters(page: Page, dialog: Locator) {
  const firstPage = entriesResponse(page);
  await dialog.getByRole("button", {name: "Apply filters", exact: true}).click();
  const response = await firstPage;
  expect(response.status()).toBe(200);
  expect(new URL(response.url()).searchParams.has("cursor")).toBe(false);
  await expect(dialog).toBeHidden();
}

async function filterBy(page: Page, edit: (dialog: Locator) => Promise<void>) {
  const dialog = await openFilters(page);
  await dialog.getByRole("button", {name: "Clear all", exact: true}).click();
  await edit(dialog);
  await applyFilters(page, dialog);
}

async function expectOnlyRows(page: Page, names: string[]) {
  const list = files(page);
  for (const name of names) await expect(list.getByText(name, {exact: true})).toBeVisible();
  await expect(list.getByRole("listitem")).toHaveCount(names.length);
}

async function expectPhoneLayout(page: Page) {
  // Mobile Chromium widens the layout viewport to fit overflowing content, so
  // compare with the emulated device width rather than window.innerWidth.
  const width = page.viewportSize()?.width ?? 0;
  expect(await page.evaluate(() => [
    document.documentElement.scrollWidth, window.innerWidth,
  ])).toEqual([width, width]);
  for (const control of await page.locator("button:visible, a:visible").all()) {
    const box = await control.boundingBox();
    expect(box?.width).toBeGreaterThanOrEqual(44);
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }
}

async function scrollFiles(page: Page, position: "top" | "middle" | "bottom") {
  await page.locator(".virtual-file-list__viewport").evaluate((element, target) => {
    element.scrollTop = target === "top" ? 0
      : target === "bottom" ? element.scrollHeight
        : (element.scrollHeight - element.clientHeight) / 2;
  }, position);
}

/**
 * Changes Bob's group grant through the real Django admin form as the
 * synthetic superuser, in a separate browser context (its own cookie jar).
 */
async function setBobGrant(page: Page, rootId: string, permissions: "0" | "1") {
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
    // Grants list their root by opaque ID; the group grant is the only one on Bob's root.
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

test("Alice browses indexed files while Bob remains isolated", async ({page}) => {
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  await page.getByRole("link", {name: /Alice files/}).click();
  await expect(page.getByRole("list", {name: "Files"})).toBeVisible();
  await expect(page.getByText("alice-only.txt", {exact: true})).toBeVisible();
  await expect(page.getByText("bob-only.txt", {exact: true})).toHaveCount(0);
  await expect(page.getByText("Index ready", {exact: true})).toBeVisible();
  await page.getByRole("button", {name: "Filters", exact: true}).click();
  await page.getByLabel("Filename starts with").fill("alice-only");
  // Task 13 named the dialog's submit control "Apply filters".
  await page.getByRole("button", {name: "Apply filters", exact: true}).click();
  await expect(page.getByText("alice-only.txt", {exact: true})).toBeVisible();
  await expect(files(page).getByRole("listitem")).toHaveCount(1);
  await page.getByRole("button", {name: "Sign out"}).click();
  await page.goBack();
  await expect(page.getByText("alice-only.txt", {exact: true})).toHaveCount(0);
});

test("real cursor paging beyond five windows and nested navigation restore", async ({page}) => {
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  await openRoot(page, "Alice files");
  const more = page.getByRole("button", {name: "Load more files"});
  const previous = page.getByRole("button", {name: "Load previous files"});
  await expect(files(page).getByText("alice-only.txt", {exact: true})).toBeVisible();
  await expect(previous).toBeDisabled();

  // 608 root entries are seven real 100-entry pages; the window keeps five.
  for (let loaded = 2; loaded <= 7; loaded += 1) {
    const next = entriesResponse(page, (url) => url.searchParams.has("cursor"));
    await more.click();
    expect((await next).status()).toBe(200);
    await expect(page.getByRole("button", {name: "Loading more files…"})).toHaveCount(0);
  }
  await expect(more).toBeDisabled();
  await expect(previous).toBeEnabled();
  await scrollFiles(page, "bottom");
  await expect(files(page).getByText("tie.txt", {exact: true})).toBeVisible();
  await expect(files(page).getByText("Tie.txt", {exact: true})).toBeVisible();
  await expect(page.getByText("alice-only.txt", {exact: true})).toHaveCount(0);

  // Leave the edge first: at an edge the list's own scroll paging may also fire.
  await scrollFiles(page, "middle");
  for (let restored = 0; restored < 2; restored += 1) {
    const before = entriesResponse(page, (url) => url.searchParams.has("cursor"));
    await previous.click();
    expect((await before).status()).toBe(200);
    await expect(page.getByRole("button", {name: "Loading previous files…"})).toHaveCount(0);
  }
  await expect(previous).toBeDisabled();
  await expect(more).toBeEnabled();
  await scrollFiles(page, "top");
  await expect(files(page).getByText("alice-only.txt", {exact: true})).toBeVisible();

  // Nested directories use real parent IDs; Back restores each level.
  const rootUrl = page.url();
  await directoryRow(page, "albums").click();
  await expect(page).toHaveURL(/\/directories\/[0-9a-f-]{36}$/);
  await expectOnlyRows(page, ["2024", "cover.png"]);
  await directoryRow(page, "2024").click();
  await directoryRow(page, "summer").click();
  await expectOnlyRows(page, ["beach.jpg", "deep.txt"]);
  await expect(page.getByRole("link", {name: "Alice files"})).toBeVisible();
  await fileRow(page, "deep.txt").click();
  const location = page.getByRole("region", {name: "File details"}).getByRole("list", {name: "Location"});
  await expect(location.getByRole("listitem")).toHaveText(["Root", "albums", "2024", "summer"]);
  await page.goBack();
  await expectOnlyRows(page, ["summer"]);
  await page.goBack();
  await page.goBack();
  await expect(page).toHaveURL(rootUrl);
  await expect(files(page).getByText("alice-only.txt", {exact: true})).toBeVisible();

  await directoryRow(page, "empty-folder").click();
  await expect(emptyNotice(page)).toHaveText("No files in this location.");
  await expect(files(page)).toHaveCount(0);
});

test.describe("filters in a DST-observing browser time zone", () => {
  test.use({timezoneId: "America/New_York"});

  test("size, date, type, kind, literal prefix and unknown metadata filters", async ({page}) => {
    await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
    await openRoot(page, "Alice files");

    // Literal prefixes: % is text, not a wildcard; matching ignores letter case.
    await filterBy(page, (dialog) => dialog.getByLabel("Filename starts with").fill("100%"));
    await expectOnlyRows(page, ["100%_done.txt"]);
    await expect(page.getByRole("button", {name: /^Remove filter /})).toHaveCount(1);
    await filterBy(page, (dialog) => dialog.getByLabel("Filename starts with").fill("TIE"));
    await expectOnlyRows(page, ["Tie.txt", "tie.txt"]);
    await filterBy(page, (dialog) => dialog.getByLabel("Filename starts with").fill("sibling"));
    await expect(emptyNotice(page)).toHaveText("No entries match these filters.");

    // The explicit unknown type and the literal .unknown extension stay distinct.
    await filterBy(page, (dialog) => dialog.getByLabel("No extension (unknown type)").check());
    await expect(files(page).getByText("README", {exact: true})).toBeVisible();
    await expect(files(page).getByText("data.unknown", {exact: true})).toHaveCount(0);
    await expect(files(page).getByText("photo.JPG", {exact: true})).toHaveCount(0);
    await filterBy(page, (dialog) => dialog.getByLabel("Other extensions").fill("unknown"));
    await expectOnlyRows(page, ["data.unknown"]);
    await filterBy(page, async (dialog) => {
      await dialog.getByLabel(".jpg", {exact: true}).check();
      await dialog.getByLabel(".pdf", {exact: true}).check();
    });
    await expectOnlyRows(page, ["photo.JPG", "résumé.pdf"]);

    await filterBy(page, async (dialog) => {
      await dialog.getByLabel("Minimum size", {exact: true}).fill("1");
      await dialog.getByLabel("Minimum size unit").selectOption("MB");
    });
    await expectOnlyRows(page, ["large.bin"]);
    await filterBy(page, (dialog) => dialog.getByLabel("Only entries with unknown size").check());
    await expect(emptyNotice(page)).toHaveText("No entries match these filters.");
    await expect(page.getByText("This location has not been indexed yet.", {exact: true})).toHaveCount(0);

    // A New York local day is [05:00Z, 04:00Z next day) across the March 8 DST change.
    await filterBy(page, async (dialog) => {
      await expect(dialog.getByText("America/New_York")).toBeVisible();
      await dialog.getByLabel("Modified on or after").fill("2026-03-08");
      await dialog.getByLabel("Modified on or before").fill("2026-03-08");
    });
    await expectOnlyRows(page, ["dst-day.txt"]);
    // A long offset-qualified date chip must not widen the phone layout.
    for (const width of [320, 390, 412]) {
      await page.setViewportSize({width, height: 844});
      await expectPhoneLayout(page);
    }
    await page.setViewportSize({width: 390, height: 844});
    await filterBy(page, (dialog) => dialog.getByLabel("Modified on or before").fill("2001-12-31"));
    await expectOnlyRows(page, ["old-report.txt"]);

    await filterBy(page, (dialog) => dialog.getByLabel("Symbolic link", {exact: true}).check());
    await expectOnlyRows(page, ["link-outside"]);
    await filterBy(page, (dialog) => dialog.getByLabel("Directory", {exact: true}).check());
    await expectOnlyRows(page, ["albums", "empty-folder"]);

    // Chip removal and Clear all return to the unfiltered first page.
    const cleared = entriesResponse(page, (url) => !url.searchParams.has("cursor"));
    await page.getByRole("button", {name: "Clear all filters"}).click();
    expect((await cleared).status()).toBe(200);
    await expect(page.getByRole("button", {name: "Filters", exact: true})).toBeFocused();
    await expect(files(page).getByText("alice-only.txt", {exact: true})).toBeVisible();
  });
});

test("details, inert links and keyboard dialog focus at phone widths", async ({page}) => {
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  await openRoot(page, "Alice files");
  const details = page.getByRole("region", {name: "File details"});

  for (const width of [320, 390, 412]) {
    await page.setViewportSize({width, height: 844});
    await expectPhoneLayout(page);
    const filters = page.getByRole("button", {name: "Filters", exact: true});
    await filters.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", {name: "Filter files"});
    await expect(dialog).toBeVisible();
    expect(await dialog.evaluate((element) => element.matches(":modal"))).toBe(true);
    expect(await dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true);
    const box = await dialog.boundingBox();
    expect(box?.width).toBeGreaterThanOrEqual(width - 1);
    for (const control of await dialog.getByRole("button").all()) {
      const controlBox = await control.boundingBox();
      expect(controlBox?.height).toBeGreaterThanOrEqual(44);
    }
    for (let step = 0; step < 4; step += 1) {
      await page.keyboard.press("Tab");
      expect(await dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true);
    }
    expect(await page.evaluate(() => window.innerWidth)).toBe(width);
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(filters).toBeFocused();
  }

  await fileRow(page, "alice-only.txt").focus();
  await page.keyboard.press("Enter");
  await expect(details.getByRole("heading", {name: "alice-only.txt"})).toBeFocused();
  await expect(details.getByText("File", {exact: true})).toBeVisible();
  await expect(details.getByText(".txt", {exact: true})).toBeVisible();
  await expect(details.getByText("24 bytes", {exact: true})).toBeVisible();
  await expect(details.getByText("Filesystem modification time, not a capture time.")).toBeVisible();
  await expect(details.getByRole("link")).toHaveCount(0);
  await details.getByRole("button", {name: "Close details"}).click();
  await expect(details).toHaveCount(0);
  await expect(fileRow(page, "alice-only.txt")).toBeFocused();

  // Unknown type metadata, exact sizes and inert symbolic links from the real catalog.
  await filterBy(page, (dialog) => dialog.getByLabel("Filename starts with").fill("data"));
  await fileRow(page, "data.unknown").click();
  await expect(details.getByText(".unknown", {exact: true})).toBeVisible();
  await filterBy(page, (dialog) => dialog.getByLabel("Filename starts with").fill("README"));
  await fileRow(page, "README").click();
  await expect(details.getByText("No extension", {exact: true})).toBeVisible();
  await filterBy(page, (dialog) => dialog.getByLabel("Filename starts with").fill("large"));
  await fileRow(page, "large.bin").click();
  await expect(details.getByText(/^1,500,000 bytes/)).toBeVisible();
  await filterBy(page, (dialog) => dialog.getByLabel("Filename starts with").fill("link"));
  await fileRow(page, "link-outside").click();
  await expect(details.getByText("Symbolic link", {exact: true})).toBeVisible();
  await expect(details.getByText(/Links are recorded as metadata and never followed/)).toBeVisible();
  await expect(page.getByText("sibling-secret.txt")).toHaveCount(0);
  await expectPhoneLayout(page);
});

test("stale cursor is refused by the server and Start from the beginning restarts at page one", async ({page}) => {
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  await openRoot(page, "Alice files");
  const next = entriesResponse(page, (url) => url.searchParams.has("cursor"));
  await page.getByRole("button", {name: "Load more files"}).click();
  const pageTwo = await next;
  expect(pageTwo.status()).toBe(200);
  const cursor = (await pageTwo.json()).nextCursor as string;
  expect(cursor).toBeTruthy();

  // A real signed cursor replayed with another filter context must restart.
  const replay = new URL(pageTwo.url());
  replay.searchParams.set("cursor", cursor);
  replay.searchParams.set("filters", JSON.stringify({v: 1, prefix: "bulk"}));
  const refused = await page.request.get(replay.pathname + replay.search);
  expect(refused.status()).toBe(409);
  expect(refused.headers()["cache-control"]).toContain("no-store");
  expect((await refused.json()).type).toBe("cursor_restart_required");

  // The browser's next real request carries a stale (altered) cursor; the real 409 is shown.
  await page.route((url) => isEntries(url) && url.searchParams.has("cursor"), async (route) => {
    const stale = new URL(route.request().url());
    stale.searchParams.set("cursor", `${stale.searchParams.get("cursor")}A`);
    await route.continue({url: stale.toString()});
  });
  const more = page.getByRole("button", {name: "Load more files"});
  const failed = entriesResponse(page, (url) => url.searchParams.has("cursor"));
  await more.click();
  expect((await failed).status()).toBe(409);
  await expect(page.getByRole("alert")).toHaveText(
    "This file list changed. Start from the beginning to keep browsing.",
  );
  // The dead retry is gone: the stale cursor cannot be resent.
  await expect(more).toBeDisabled();
  await page.unrouteAll({behavior: "wait"});

  // The real recovery path: one uncursored page-one request, then paging resumes.
  const restarted = entriesResponse(page);
  await page.getByRole("button", {name: "Start from the beginning"}).click();
  const firstPage = await restarted;
  expect(firstPage.status()).toBe(200);
  expect(new URL(firstPage.url()).searchParams.has("cursor")).toBe(false);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.getByRole("button", {name: "Filters", exact: true})).toBeFocused();
  await scrollFiles(page, "top");
  await expect(files(page).getByText("alice-only.txt", {exact: true})).toBeVisible();
  const resumed = entriesResponse(page, (url) => url.searchParams.has("cursor"));
  await more.click();
  expect((await resumed).status()).toBe(200);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("late responses after logout, account switch and grant revocation stay private", async ({page}) => {
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  await openRoot(page, "Alice files");
  const aliceRoot = new URL(page.url()).pathname;

  // Hold real details and next-page requests until after sign-out.
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const settled: Promise<void>[] = [];
  await page.route((url) => url.pathname.startsWith("/api/v1/entries/")
    || (isEntries(url) && url.searchParams.has("cursor")), (route) => {
    const handled = gate.then(() => route.continue()).catch(() => undefined);
    settled.push(handled);
    return handled;
  });
  const heldDetails = page.waitForRequest((request) =>
    new URL(request.url()).pathname.startsWith("/api/v1/entries/"));
  await fileRow(page, "alice-only.txt").click();
  await heldDetails;
  const heldPage = page.waitForRequest((request) => {
    const url = new URL(request.url());
    return isEntries(url) && url.searchParams.has("cursor");
  });
  await page.getByRole("button", {name: "Load more files"}).click();
  await heldPage;
  await page.getByRole("button", {name: "Sign out"}).click();
  await expect(page.getByRole("heading", {name: "Sign in"})).toBeVisible();
  release();
  await Promise.all(settled);
  await page.unrouteAll({behavior: "wait"});
  await expect(page.getByRole("button", {name: "Sign in"})).toBeEnabled();
  await expect(page.getByRole("heading", {name: "Sign in"})).toBeVisible();
  await expect(page.getByText(/alice-only\.txt|bulk-0|Alice files/)).toHaveCount(0);
  await expect.poll(async () => (await page.request.get("/api/v1/auth/session")).status())
    .toBe(401);

  // Account switch in the same tab: Bob sees only his degraded root.
  await signIn(page, "bob", requiredSecret("E2E_BOB_PASSWORD"));
  await expect(page.getByText("Alice files")).toHaveCount(0);
  await openRoot(page, "Bob files");
  const bobRoot = new URL(page.url()).pathname.split("/")[2]!;
  await expect(files(page).getByText("bob-only.txt", {exact: true})).toBeVisible();
  await expect(page.getByText("alice-only.txt", {exact: true})).toHaveCount(0);
  await expect(page.getByText("Indexed with problems", {exact: true})).toBeVisible();
  await expect(directoryRow(page, "locked")).toBeVisible();
  await page.goto(aliceRoot);
  await expect(page.getByRole("heading", {name: "Root unavailable"})).toBeVisible();
  await expect(page.getByText(/alice-only\.txt|Alice files/)).toHaveCount(0);
  await page.goBack();
  await expect(files(page).getByText("bob-only.txt", {exact: true})).toBeVisible();

  // Revoking Bob's grant closes the open private view on its next request.
  try {
    await setBobGrant(page, bobRoot, "0");
    await fileRow(page, "bob-only.txt").click();
    await expect(page.getByRole("heading", {name: "Sign in"})).toBeVisible();
    await expect(page.getByText(/bob-only\.txt|Bob files/)).toHaveCount(0);
    await signIn(page, "bob", requiredSecret("E2E_BOB_PASSWORD"));
    await expect(page.getByRole("heading", {name: "No roots assigned"})).toBeVisible();
    await expect(page.getByText("Bob files")).toHaveCount(0);
  } finally {
    await setBobGrant(page, bobRoot, "1");
  }
});
