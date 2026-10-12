import {createHash} from "node:crypto";
import {expect, type Locator, type Page} from "@playwright/test";
import {apiSignIn, apiSignOut, csrfToken, requiredSecret, signIn, submitLogin} from "./auth";
import {setRootGrant} from "./grants";
import {recordPlayback} from "./playback-record";
import {test} from "./safe-test";

// Real-stack media journeys over the harness's generated fixtures: photos made with Pillow and
// pillow-heif, clips encoded by the aegis-media image. Every thumbnail, preview, transcode and
// original comes from the real media worker, processor, API and gateway; no route is added here.
// The harness's media-wait phase has already waited (bounded) for the backfill. Serial: one
// failed journey ends this file at once instead of stacking timeouts. Nothing here skips: a
// browser that cannot decode a codec takes an explicit, asserted branch.
test.describe.configure({mode: "serial", timeout: 240_000});

const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const ENTRIES_PATH = /^\/api\/v1\/roots\/[0-9a-f-]{36}\/entries$/;
const DERIVATIVE_REQUEST = /^\/api\/v1\/entries\/([0-9a-f-]{36})\/media\/requests$/;
const SESSION_PATH = "/api/v1/auth/session";
// Every attribute through which the app can put a media URL in the page.
const MEDIA_ATTRIBUTES = ["src", "poster", "srcset", "href"];
const MEDIA_ELEMENTS = [
  'img[src*="/media/v1/"]', 'video[src*="/media/v1/"]', 'source[src*="/media/v1/"]',
  '[poster*="/media/v1/"]', '[srcset*="/media/v1/"]', 'a[href*="/media/v1/"]',
].join(", ");
const ALICE_TEXT = /photo\.JPG|phone\.heic|clip\.mp4|clip-hevc\.mov|Alice files/;
// The fixture's GPS position in DMS seconds and in decimal degrees, and any location wording.
const COORDINATES = /56[.,]789|54[.,]321|12[.,]58|98[.,]13|°|latitude|longitude|\bGPS\b/i;
const ALICE_PERMISSIONS = "7"; // BROWSE | PREVIEW | EXPORT, as seeded.
const SANITY_TIMEOUT_MS = 15_000;
const PREPARE_TIMEOUT_MS = 90_000;
const RECORDS_KEY = "aegis-e2e-media-records";

type ThumbnailState = "pending" | "ready" | "failed" | "unsupported" | "quarantined";

type TimelineItem = {
  id: string;
  displayName: string;
  media: {
    kind: "photo" | "video";
    thumbnail: ThumbnailState;
    sortDate: string;
    sortDateSource: "captured" | "modified";
    width: number | null;
    height: number | null;
    durationMs: number | null;
    thumbnailUrl: string | null;
  };
};

type DerivativeKind = "thumbnail" | "preview" | "poster" | "transcode";
type DerivativeState = "missing" | "pending" | "ready" | "failed" | "unsupported" | "quarantined";

type MediaDetails = {
  kind: "photo" | "video";
  metadata: {width: number | null; height: number | null; capturedAt: string | null; hasLocation: boolean};
  derivatives: Partial<Record<DerivativeKind, {state: DerivativeState; url: string | null}>>;
  original: {inlineUrl: string | null; downloadUrl: string | null; playbackType: string | null};
};

// Every generated fixture and the thumbnail states its real processing settles in; the
// harness's media-wait phase (scripts/e2e_support.py MEDIA_SETTLED) waits for the same.
const SETTLED: Record<string, readonly ThumbnailState[]> = {
  "photo.JPG": ["ready"],
  "phone.heic": ["ready"],
  "cover.png": ["ready"],
  "beach.jpg": ["ready"],
  "clip.mp4": ["ready"],
  "clip-hevc.mov": ["ready"],
  // The header decodes but the scan data stops halfway: the thumbnail fails (and is retried).
  "corrupt.jpg": ["failed", "quarantined"],
};

// Newest first: EXIF capture times, container creation times, then two modification times.
const MONTHS = [
  "July 2024 · Captured",
  "June 2024 · Captured",
  "May 2024 · Captured",
  "December 2023 · Captured",
  "November 2023 · Captured",
  "January 2020 · Modified",
];

function fixtureDigest(): string {
  const digest = process.env.E2E_PHOTO_SHA256 ?? "";
  if (!/^[0-9a-f]{64}$/.test(digest)) throw new Error("E2E_PHOTO_SHA256 is required");
  return digest;
}

async function aliceRootId(page: Page): Promise<string> {
  const response = await page.request.get("/api/v1/roots");
  expect(response.status()).toBe(200);
  const {roots} = await response.json() as {roots: {id: string; displayName: string}[]};
  const root = roots.find((candidate) => candidate.displayName === "Alice files");
  if (root === undefined) throw new Error("Alice's root is not listed");
  return root.id;
}

/** The real timeline, once every generated fixture is settled (media-wait already waited). */
async function mediaItems(page: Page, rootId: string): Promise<Map<string, TimelineItem>> {
  let items = new Map<string, TimelineItem>();
  await expect.poll(async () => {
    const response = await page.request.get(`/api/v1/roots/${rootId}/timeline?limit=120`);
    if (response.status() !== 200) return `status ${response.status()}`;
    const body = await response.json() as {items: TimelineItem[]};
    items = new Map(body.items.map((item) => [item.displayName, item]));
    const waiting = Object.entries(SETTLED).filter(([name, states]) => {
      const state = items.get(name)?.media.thumbnail;
      return state === undefined || !states.includes(state);
    });
    return waiting.length === 0 ? "settled" : `${waiting.length} waiting`;
  }, {timeout: SANITY_TIMEOUT_MS, intervals: [500, 1_000]}).toBe("settled");
  return items;
}

function named(items: Map<string, TimelineItem>, name: string): TimelineItem {
  const item = items.get(name);
  if (item === undefined) throw new Error("A generated fixture is missing from the timeline");
  return item;
}

async function mediaDetails(page: Page, entryId: string): Promise<MediaDetails> {
  const response = await page.request.get(`/api/v1/entries/${entryId}/media`);
  expect(response.status()).toBe(200);
  return await response.json() as MediaDetails;
}

/** Every URL a viewing of this photo involves: thumbnail, preview, inline and attachment. */
function photoUrls(details: MediaDetails): string[] {
  const urls = [
    details.derivatives.thumbnail?.url, details.derivatives.preview?.url,
    details.original.inlineUrl, details.original.downloadUrl,
  ];
  for (const url of urls) expect(url).toMatch(/^\/media\/v1\//);
  return urls.map((url) => url ?? "");
}

/** Polls the details until a derivative is ready; returns its delivery URL. */
async function readyDerivativeUrl(page: Page, entryId: string, kind: DerivativeKind): Promise<string> {
  const found = {url: null as string | null};
  await expect.poll(async () => {
    const derivative = (await mediaDetails(page, entryId)).derivatives[kind];
    found.url = derivative?.state === "ready" ? derivative.url : null;
    return derivative?.state;
  }, {timeout: PREPARE_TIMEOUT_MS, intervals: [500, 1_000, 2_000]}).toBe("ready");
  if (found.url === null) throw new Error("A ready derivative has no URL");
  return found.url;
}

type DerivativePost = {entryId: string; kind: unknown};

/** Every derivative request the page itself sends (a listener, never a route). */
function collectDerivativePosts(page: Page): DerivativePost[] {
  const posts: DerivativePost[] = [];
  page.on("request", (request) => {
    const match = DERIVATIVE_REQUEST.exec(new URL(request.url()).pathname);
    if (request.method() !== "POST" || match === null) return;
    let kind: unknown = null;
    try {
      kind = (request.postDataJSON() as {kind?: unknown} | null)?.kind ?? null;
    } catch {
      kind = null;
    }
    posts.push({entryId: match[1] ?? "", kind});
  });
  return posts;
}

function postedKinds(posts: DerivativePost[], entryId: string): unknown[] {
  return posts.filter((post) => post.entryId === entryId).map((post) => post.kind);
}

/** The same interactive request the viewer sends, through the API with this session. */
async function requestTranscode(page: Page, entryId: string, prior: DerivativeState | undefined) {
  const response = await page.request.post(`/api/v1/entries/${entryId}/media/requests`, {
    headers: {"X-CSRFToken": await csrfToken(page)},
    data: {kind: "transcode"},
  });
  expect(response.status()).toBe(202);
  const body = await response.json() as {state: string; kind: string};
  expect(body.kind).toBe("transcode");
  expect(prior === "ready" ? ["ready"] : ["pending", "ready"]).toContain(body.state);
  return await readyDerivativeUrl(page, entryId, "transcode");
}

function magic(body: Buffer): string {
  return body.subarray(4, 8).toString("latin1");
}

/** An inline original, ranged: no decoder needed, so every browser checks it. */
async function expectRangedOriginal(page: Page, url: string | null, type: string) {
  expect(url).toMatch(/^\/media\/v1\/originals\//);
  const response = await page.request.get(url ?? "", {headers: {Range: "bytes=0-63"}});
  expect(response.status()).toBe(206);
  const headers = response.headers();
  expect(headers["content-range"]).toMatch(/^bytes 0-63\/[1-9][0-9]*$/);
  expect(headers["content-type"]).toBe(type);
  expect(headers["cache-control"]).toBe("private, no-store");
  expect(headers["x-content-type-options"]).toBe("nosniff");
  expect(headers["content-disposition"]).toMatch(/^inline; filename="/);
  expect(magic(await response.body())).toBe("ftyp");
}

/** A transcode, ranged and revalidated: an H.264 MP4 the browser may cache only privately. */
async function expectTranscodeDelivery(page: Page, url: string) {
  expect(url).toMatch(new RegExp(`^/media/v1/derivatives/${UUID}/transcode\\?`));
  const ranged = await page.request.get(url, {headers: {Range: "bytes=0-63"}});
  expect(ranged.status()).toBe(206);
  const headers = ranged.headers();
  expect(headers["content-range"]).toMatch(/^bytes 0-63\/[1-9][0-9]*$/);
  expect(headers["content-type"]).toBe("video/mp4");
  expect(headers["cache-control"]).toBe("private, no-cache");
  expect(headers["etag"]).toMatch(/^"[0-9a-f]{64}\.[0-9]+\.[0-9]+"$/);
  expect(headers["vary"]).toMatch(/\bCookie\b/i);
  expect(magic(await ranged.body())).toBe("ftyp");
  const revalidated = await page.request.get(url, {headers: {"If-None-Match": headers["etag"]}});
  expect(revalidated.status()).toBe(304);
}

async function canPlay(page: Page, type: string): Promise<boolean> {
  return await page.evaluate((value) => document.createElement("video").canPlayType(value), type) !== "";
}

/** Whether an image at `url` loads in the page now (through the browser's HTTP cache rules). */
async function loadsInPage(page: Page, url: string): Promise<boolean> {
  return await page.evaluate((source) => new Promise<boolean>((resolve) => {
    const image = new Image();
    image.onload = () => resolve(true);
    image.onerror = () => resolve(false);
    image.src = source;
  }), url);
}

/** The decoded width of an image element, or 0 (also while it is replaced or detached). */
async function naturalWidth(image: Locator): Promise<number> {
  try {
    return await image.evaluate((element) => {
      const picture = element as HTMLImageElement;
      return picture.complete ? picture.naturalWidth : 0;
    }, undefined, {timeout: 5_000});
  } catch {
    return 0;
  }
}

async function expectLoaded(image: Locator) {
  await expect(image).toBeVisible();
  await expect.poll(() => naturalWidth(image), {timeout: 30_000}).toBeGreaterThan(0);
}

async function readyState(video: Locator): Promise<number> {
  try {
    return await video.evaluate((element) => (element as HTMLVideoElement).readyState, undefined, {timeout: 5_000});
  } catch {
    return -1;
  }
}

function fileRow(page: Page, name: string): Locator {
  return page.getByRole("button", {name: `Select file ${name}`, exact: true});
}

function directoryRow(page: Page, name: string): Locator {
  return page.getByRole("button", {name: `Open directory ${name}`, exact: true});
}

function photoTile(page: Page, name: string): Locator {
  return page.getByRole("link", {name: `Open photo ${name}`, exact: true});
}

function rootViews(page: Page): Locator {
  return page.getByRole("navigation", {name: "Root views"});
}

async function openFiles(page: Page, rootId: string) {
  const firstPage = page.waitForResponse((response) =>
    ENTRIES_PATH.test(new URL(response.url()).pathname) && response.request().method() === "GET");
  await page.goto(`/files/${rootId}`);
  expect((await firstPage).status()).toBe(200);
  await expect(page.getByRole("list", {name: "Files"})).toBeVisible();
}

/** From the roots list: Alice's root, then its Photos view, with a thumbnail loaded. */
async function openAlicePhotos(page: Page, rootId: string) {
  await page.getByRole("link", {name: /Alice files/}).click();
  await expect(page).toHaveURL(new RegExp(`/files/${rootId}$`));
  await rootViews(page).getByRole("link", {name: "Photos", exact: true}).click();
  await expect(page).toHaveURL(new RegExp(`/files/${rootId}/photos$`));
  await expectLoaded(photoTile(page, "photo.JPG").locator("img.media-thumbnail__image"));
}

/** Shows only the current directory's entries whose names start with `prefix`. */
async function showOnly(page: Page, prefix: string) {
  await page.getByRole("button", {name: "Filters", exact: true}).click();
  const dialog = page.getByRole("dialog", {name: "Filter files"});
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", {name: "Clear all", exact: true}).click();
  await dialog.getByRole("radio", {name: "This folder", exact: true}).check();
  await dialog.getByLabel("Filename starts with").fill(prefix);
  const firstPage = page.waitForResponse((response) =>
    ENTRIES_PATH.test(new URL(response.url()).pathname) && response.request().method() === "GET");
  await dialog.getByRole("button", {name: "Apply filters", exact: true}).click();
  expect((await firstPage).status()).toBe(200);
  await expect(dialog).toBeHidden();
}

/** A Files row whose ready thumbnail is the real derivative and has loaded. */
async function expectThumbnail(page: Page, name: string) {
  const row = fileRow(page, name);
  await expect(row).toHaveAccessibleDescription(/^(Photo|Video) thumbnail$/);
  const image = row.locator("img.media-thumbnail__image");
  await expect(image).toHaveAttribute(
    "src", new RegExp(`^/media/v1/derivatives/${UUID}/thumbnail\\?f=[0-9a-f]{32}&n=[0-9A-Za-z]+&e=[0-9]+$`),
  );
  await expectLoaded(image);
}

/**
 * The virtualized timeline's month headings, top to bottom. Each heading is keyed by its row
 * offset, so a repeated group shows up as an extra entry, and collection repeats until the
 * rows rendered after the last scroll complete the expected order.
 */
async function expectMonthHeadings(page: Page, expected: readonly string[]) {
  const viewport = page.locator(".timeline-grid__viewport");
  const found = new Map<number, string>();
  const collect = async () => {
    const rendered = await page.locator(".timeline-grid__month").evaluateAll((elements) =>
      elements.map((element) => {
        const offset = /translateY\((-?[0-9.]+)px\)/.exec((element as HTMLElement).style.transform);
        return [offset ? Number(offset[1]) : Number.NaN, element.textContent ?? ""] as [number, string];
      }));
    for (const [offset, text] of rendered) if (Number.isFinite(offset)) found.set(offset, text);
  };
  const ordered = () => [...found.entries()].sort(([left], [right]) => left - right).map(([, text]) => text);
  await viewport.evaluate((element) => {
    element.scrollTop = 0;
  });
  await expect.poll(async () => {
    await collect();
    return await viewport.evaluate((element) => {
      const end = element.scrollTop + element.clientHeight >= element.scrollHeight - 1;
      if (!end) element.scrollTop += Math.max(1, Math.floor(element.clientHeight / 3));
      return end;
    });
  }, {timeout: 30_000, intervals: [100]}).toBe(true);
  await expect.poll(async () => {
    await collect();
    return ordered();
  }, {timeout: 10_000}).toEqual([...expected]);
}

/** One synthetic touch gesture on the photo stage: a horizontal swipe. */
async function swipe(stage: Locator, direction: "left" | "right") {
  await stage.evaluate((element, toLeft) => {
    const box = element.getBoundingClientRect();
    const y = box.top + box.height / 2;
    const start = box.left + box.width * (toLeft ? 0.75 : 0.25);
    const end = box.left + box.width * (toLeft ? 0.25 : 0.75);
    const send = (type: string, x: number) => element.dispatchEvent(new PointerEvent(type, {
      bubbles: true, cancelable: true, composed: true, pointerId: 21, pointerType: "touch",
      isPrimary: true, button: 0, buttons: type === "pointerup" ? 0 : 1, clientX: x, clientY: y,
    }));
    send("pointerdown", start);
    send("pointermove", (start + end) / 2);
    send("pointermove", end);
    send("pointerup", end);
  }, direction === "left");
}

/** Two quick synthetic touch taps on the middle of the photo stage. */
async function doubleTap(stage: Locator) {
  await stage.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const x = box.left + box.width / 2;
    const y = box.top + box.height / 2;
    for (const pointerId of [31, 32]) {
      for (const type of ["pointerdown", "pointerup"]) {
        element.dispatchEvent(new PointerEvent(type, {
          bubbles: true, cancelable: true, composed: true, pointerId, pointerType: "touch",
          isPrimary: true, button: 0, buttons: type === "pointerdown" ? 1 : 0, clientX: x, clientY: y,
        }));
      }
    }
  });
}

/** Current playback position once the element has a decoded frame, or -1; starts muted playback. */
async function playbackPosition(video: Locator): Promise<number> {
  try {
    return await video.evaluate((element) => {
      const media = element as HTMLVideoElement;
      media.muted = true;
      if (media.paused && !media.ended) void media.play().catch(() => undefined);
      return media.readyState >= 2 ? media.currentTime : -1;
    }, undefined, {timeout: 5_000});
  } catch {
    return -1;
  }
}

/** The element reaches HAVE_CURRENT_DATA and its playback position advances. */
async function expectPlays(video: Locator) {
  await expect.poll(() => playbackPosition(video), {timeout: PREPARE_TIMEOUT_MS}).toBeGreaterThan(0.2);
  expect(await readyState(video)).toBeGreaterThanOrEqual(2);
}

async function expectSeeks(video: Locator, seconds: number) {
  const position = await video.evaluate((element, target) => new Promise<number>((resolve) => {
    const media = element as HTMLVideoElement;
    media.pause();
    const timer = setTimeout(() => resolve(-1), 15_000);
    media.addEventListener("seeked", () => {
      clearTimeout(timer);
      resolve(media.currentTime);
    }, {once: true});
    media.currentTime = target;
  }), seconds);
  expect(Math.abs(position - seconds)).toBeLessThan(0.3);
  await expect.poll(() => readyState(video)).toBeGreaterThanOrEqual(2);
}

/**
 * The viewer chose a transcode: "Preparing playback…" and exactly one request when it was
 * missing, no request when it was ready, and a video element given the transcode URL (read
 * from the recorder: a browser that cannot decode it removes the element at once).
 */
async function expectTranscodeChoice(
  page: Page, stage: Locator, entryId: string, prior: DerivativeState | undefined, posts: DerivativePost[],
): Promise<string> {
  expect(["missing", "ready"]).toContain(prior);
  if (prior === "missing") await expect(stage.getByRole("status")).toHaveText("Preparing playback…");
  await expect(stage).toHaveAttribute("data-source", "transcode", {timeout: PREPARE_TIMEOUT_MS});
  const url = await readyDerivativeUrl(page, entryId, "transcode");
  await expect.poll(() => mediaShownSince(page, 0), {timeout: PREPARE_TIMEOUT_MS}).toContain(url);
  expect(postedKinds(posts, entryId)).toEqual(prior === "missing" ? ["transcode"] : []);
  return url;
}

/** A browser without the decoder: the honest final state, with Retry and no video element. */
async function expectPlaybackUnavailable(stage: Locator) {
  await expect(stage.getByRole("status")).toHaveText("This video could not be played.", {timeout: PREPARE_TIMEOUT_MS});
  await expect(stage.getByRole("button", {name: "Retry playback", exact: true})).toBeVisible();
  await expect(stage).toHaveAttribute("data-source", "transcode");
  await expect(stage.locator("video")).toHaveCount(0);
}

/**
 * Records every media URL that any document of this tab puts in a media attribute, with its
 * time, in memory and in this tab's sessionStorage (which the app never uses), so one check
 * covers every document the tab loaded.
 */
async function recordMediaElements(page: Page) {
  await page.addInitScript(({key, attributes}) => {
    const memory: {src: string; at: number}[] = [];
    Object.defineProperty(window, "__aegisMediaRecords", {value: memory});
    const remember = (value: string) => {
      const record = {src: value, at: Date.now()};
      memory.push(record);
      try {
        const stored = JSON.parse(sessionStorage.getItem(key) ?? "[]") as unknown;
        const list = Array.isArray(stored) ? stored : [];
        list.push(record);
        sessionStorage.setItem(key, JSON.stringify(list.slice(-500)));
      } catch {
        // No storage in this document (an opaque one); the in-memory list still holds it.
      }
    };
    const note = (element: Element) => {
      for (const name of attributes) {
        const value = element.getAttribute(name);
        if (value !== null && value.includes("/media/v1/")) remember(value);
      }
    };
    const visit = (node: Node) => {
      if (!(node instanceof Element)) return;
      note(node);
      node.querySelectorAll(attributes.map((name) => `[${name}]`).join(", ")).forEach(note);
    };
    new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type === "attributes") visit(mutation.target);
        else mutation.addedNodes.forEach(visit);
      }
    }).observe(document, {subtree: true, childList: true, attributes: true, attributeFilter: attributes});
  }, {key: RECORDS_KEY, attributes: MEDIA_ATTRIBUTES});
}

async function mediaShownSince(page: Page, since: number): Promise<string[]> {
  return await page.evaluate(({key, cutoff}) => {
    type MediaRecord = {src: string; at: number};
    const memory = (window as unknown as {__aegisMediaRecords?: MediaRecord[]}).__aegisMediaRecords ?? [];
    let stored: unknown = [];
    try {
      stored = JSON.parse(sessionStorage.getItem(key) ?? "[]");
    } catch {
      stored = [];
    }
    const records = [...memory, ...(Array.isArray(stored) ? stored as MediaRecord[] : [])];
    return [...new Set(records.filter((record) => record.at >= cutoff).map((record) => record.src))];
  }, {key: RECORDS_KEY, cutoff: since});
}

/** Nothing private on screen now, and no media URL attached by any document since `since`. */
async function expectNoPrivateMedia(page: Page, since: number) {
  await expect(page.locator(MEDIA_ELEMENTS)).toHaveCount(0);
  await expect(page.getByText(ALICE_TEXT)).toHaveCount(0);
  expect(await mediaShownSince(page, since)).toEqual([]);
}

async function expectAllRefused(page: Page, urls: string[]) {
  for (const url of urls) expect((await page.request.get(url)).status()).toBe(404);
}

async function restorePageFromHistoryCache(page: Page) {
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pageshow", {persisted: true})));
}

test("generated media gets thumbnails in Files and month groups in Photos", async ({page}) => {
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  const rootId = await aliceRootId(page);
  const items = await mediaItems(page, rootId);
  const photo = named(items, "photo.JPG");
  // Display dimensions: stored 160 × 120 with EXIF orientation 6, so shown portrait.
  expect([photo.media.width, photo.media.height]).toEqual([120, 160]);
  expect(photo.media.sortDateSource).toBe("captured");
  expect(named(items, "cover.png").media.sortDateSource).toBe("modified");
  expect(named(items, "clip.mp4").media.durationMs).toBeGreaterThan(1_500);
  expect(named(items, "corrupt.jpg").media.thumbnailUrl).toBeNull();

  await openFiles(page, rootId);
  await directoryRow(page, "albums").click();
  await expectThumbnail(page, "cover.png");
  await directoryRow(page, "2024").click();
  await directoryRow(page, "summer").click();
  await expectThumbnail(page, "beach.jpg");

  await openFiles(page, rootId);
  for (const [prefix, names] of [
    ["photo", ["photo.JPG"]],
    ["phone", ["phone.heic"]],
    ["clip", ["clip-hevc.mov", "clip.mp4"]],
  ] as const) {
    await showOnly(page, prefix);
    for (const name of names) await expectThumbnail(page, name);
  }
  await showOnly(page, "corrupt");
  const corrupt = fileRow(page, "corrupt.jpg");
  await expect(corrupt).toHaveAccessibleDescription("Photo, thumbnail could not be created");
  await expect(corrupt.locator("img")).toHaveCount(0);

  await rootViews(page).getByRole("link", {name: "Photos", exact: true}).click();
  await expect(page).toHaveURL(new RegExp(`/files/${rootId}/photos$`));
  await expectLoaded(photoTile(page, "photo.JPG").locator("img.media-thumbnail__image"));
  await expectMonthHeadings(page, MONTHS);
});

test("the viewer prepares a preview, swipes, follows arrow keys, zooms and returns focus", async ({page}) => {
  const posts = collectDerivativePosts(page);
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  const rootId = await aliceRootId(page);
  const items = await mediaItems(page, rootId);
  // Timeline neighbors: clip.mp4 (June 2024) before photo.JPG (May 2024), phone.heic after.
  const photo = named(items, "photo.JPG");
  const next = named(items, "phone.heic");
  const before = await mediaDetails(page, photo.id);
  // Built on demand: missing for the first browser of a run, ready (and reused) after that.
  const prior = before.derivatives.preview?.state;
  expect(["missing", "ready"]).toContain(prior);

  await page.goto(`/files/${rootId}/photos`);
  const tile = photoTile(page, "photo.JPG");
  await expectLoaded(tile.locator("img.media-thumbnail__image"));
  await tile.click();
  const viewer = page.getByRole("dialog");
  const title = viewer.getByRole("heading", {level: 2});
  await expect(viewer).toBeVisible();
  await expect(title).toHaveText("photo.JPG");
  await expect(page).toHaveURL(new RegExp(`/files/${rootId}/view/${photo.id}$`));
  const preview = viewer.locator("img.photo-stage__image[data-loaded]");
  await expect(preview).toHaveAttribute(
    "src", new RegExp(`^/media/v1/derivatives/${photo.id}/preview\\?`), {timeout: PREPARE_TIMEOUT_MS},
  );
  await expectLoaded(preview);
  if (prior === "missing") {
    expect(postedKinds(posts, photo.id)).toEqual(["preview"]);
  } else {
    expect(postedKinds(posts, photo.id)).toEqual([]);
    await expect(preview).toHaveAttribute("src", before.derivatives.preview?.url ?? "");
  }

  const stage = viewer.getByTestId("photo-stage");
  await swipe(stage, "left");
  await expect(page).toHaveURL(new RegExp(`/view/${next.id}$`));
  await expect(title).toHaveText("phone.heic");
  await swipe(stage, "right");
  await expect(page).toHaveURL(new RegExp(`/view/${photo.id}$`));
  await expect(title).toHaveText("photo.JPG");

  await page.keyboard.press("ArrowRight");
  await expect(page).toHaveURL(new RegExp(`/view/${next.id}$`));
  await expect(title).toHaveText("phone.heic");
  await page.keyboard.press("ArrowLeft");
  await expect(page).toHaveURL(new RegExp(`/view/${photo.id}$`));
  await expect(title).toHaveText("photo.JPG");

  await expectLoaded(viewer.locator("img.photo-stage__image[data-loaded]"));
  await doubleTap(stage);
  await expect(stage).toHaveAttribute("data-scale", "2.5");
  await expect(stage).toHaveAttribute("data-zoomed", "true");
  await doubleTap(stage);
  await expect(stage).toHaveAttribute("data-scale", "1");
  await expect(stage).not.toHaveAttribute("data-zoomed");

  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/files/${rootId}/photos$`));
  await expect(tile).toBeFocused();
});

test("videos: the viewer's choice, ranged delivery, transcodes, and playback wherever the browser decodes", async ({page}, testInfo) => {
  await recordMediaElements(page);
  const posts = collectDerivativePosts(page);
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  const rootId = await aliceRootId(page);
  const items = await mediaItems(page, rootId);
  const mp4 = named(items, "clip.mp4");
  const mov = named(items, "clip-hevc.mov");
  const mp4Before = await mediaDetails(page, mp4.id);
  const movBefore = await mediaDetails(page, mov.id);
  const mp4Type = mp4Before.original.playbackType ?? "";
  const movType = movBefore.original.playbackType ?? "";
  expect(mp4Type).toMatch(/^video\/mp4; codecs="avc1\.[0-9A-F]{6}, mp4a\.40\.2"$/);
  expect(movType).toMatch(/^video\/quicktime; codecs="hvc1\.[0-9A-Za-z.]+, mp4a\.40\.2"$/);
  // Delivery needs no decoder: both originals, ranged, in every browser.
  await expectRangedOriginal(page, mp4Before.original.inlineUrl, "video/mp4");
  await expectRangedOriginal(page, movBefore.original.inlineUrl, "video/quicktime");

  await page.goto(`/files/${rootId}/photos`);
  const h264 = await canPlay(page, mp4Type);
  const hevc = await canPlay(page, movType);
  let played = false;
  const filter = page.getByRole("group", {name: "Show"}).getByRole("button", {name: "Videos", exact: true});
  await filter.click();
  await expect(filter).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("link", {name: /^Open video clip\.mp4(, \d+:\d{2})?$/}).click();
  await expect(page).toHaveURL(new RegExp(`/view/${mp4.id}$`));
  const stage = page.getByTestId("video-stage");
  if (h264) {
    await expect(stage).toHaveAttribute("data-source", "original");
    const video = stage.locator("video");
    await expect(video).toHaveAttribute("src", mp4Before.original.inlineUrl ?? "");
    await expectPlays(video);
    await expectSeeks(video, 1);
    expect(postedKinds(posts, mp4.id)).toEqual([]);
    played = true;
  } else {
    // Every transcode is H.264 too: the viewer asks for one, which this browser cannot play.
    const url = await expectTranscodeChoice(page, stage, mp4.id, mp4Before.derivatives.transcode?.state, posts);
    await expectPlaybackUnavailable(stage);
    await expectTranscodeDelivery(page, url);
  }

  await page.getByRole("button", {name: "Next video", exact: true}).click();
  await expect(page).toHaveURL(new RegExp(`/view/${mov.id}$`));
  const movStage = page.getByTestId("video-stage");
  const movVideo = movStage.locator("video");
  const movPrior = movBefore.derivatives.transcode?.state;
  let movSource: string;
  let transcodeUrl: string;
  if (hevc) {
    // The browser claims HEVC: the original plays, unless decoding fails and the viewer falls
    // back to a transcode.
    const outcome = {source: ""};
    await expect.poll(async () => {
      const source = await movStage.getAttribute("data-source");
      if (source === "transcode") outcome.source = "transcode";
      else if (source === "original" && await playbackPosition(movVideo) > 0.2) outcome.source = "original";
      return outcome.source;
    }, {timeout: PREPARE_TIMEOUT_MS}).not.toBe("");
    movSource = outcome.source;
    if (movSource === "original") {
      await expect(movVideo).toHaveAttribute("src", movBefore.original.inlineUrl ?? "");
      await expectPlays(movVideo);
      expect(postedKinds(posts, mov.id)).toEqual([]);
      played = true;
    } else {
      const fallback = await readyDerivativeUrl(page, mov.id, "transcode");
      await expect.poll(() => mediaShownSince(page, 0), {timeout: PREPARE_TIMEOUT_MS}).toContain(fallback);
      if (h264) {
        await expect(movVideo).toHaveAttribute("src", fallback);
        await expectPlays(movVideo);
        played = true;
      } else {
        await expectPlaybackUnavailable(movStage);
      }
    }
    // The viewer did not take the transcode path for this clip: exercise it through the API.
    transcodeUrl = await requestTranscode(page, mov.id, movPrior);
  } else {
    movSource = "transcode";
    transcodeUrl = await expectTranscodeChoice(page, movStage, mov.id, movPrior, posts);
    if (h264) {
      await expect(movVideo).toHaveAttribute("src", transcodeUrl);
      await expectPlays(movVideo);
      played = true;
    } else {
      await expectPlaybackUnavailable(movStage);
    }
  }
  await expectTranscodeDelivery(page, transcodeUrl);
  testInfo.annotations.push({type: "video-branch", description: `h264=${h264} hevc=${hevc} mov=${movSource}`});
  // The global teardown requires at least one browser of the run to have proved playback.
  await recordPlayback(testInfo, played);
});

test("Download original sends the fixture's exact bytes as a named attachment", async ({page}) => {
  const expected = fixtureDigest();
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  const rootId = await aliceRootId(page);
  await mediaItems(page, rootId);
  await openFiles(page, rootId);
  await showOnly(page, "photo");
  await fileRow(page, "photo.JPG").click();
  const details = page.getByRole("region", {name: "File details"});
  const download = details.getByRole("link", {name: "Download original", exact: true});
  await expect(download).toHaveAttribute("download", "");
  const href = await download.getAttribute("href");
  expect(href).toMatch(new RegExp(`^/media/v1/originals/${UUID}\\?d=attachment&n=[0-9A-Za-z]+&e=[0-9]+$`));

  // The browser context's own cookies, without a download dialog.
  const response = await page.request.get(href ?? "");
  expect(response.status()).toBe(200);
  const headers = response.headers();
  expect(headers["content-disposition"]).toBe(`attachment; filename="photo.JPG"; filename*=UTF-8''photo.JPG`);
  expect(headers["content-type"]).toBe("image/jpeg");
  expect(headers["cache-control"]).toBe("private, no-store");
  expect(headers["x-content-type-options"]).toBe("nosniff");
  expect(createHash("sha256").update(await response.body()).digest("hex")).toBe(expected);
});

test("details show the capture time, display dimensions and never coordinates", async ({page}) => {
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  const rootId = await aliceRootId(page);
  const photo = named(await mediaItems(page, rootId), "photo.JPG");
  const api = await page.request.get(`/api/v1/entries/${photo.id}/media`);
  expect(api.status()).toBe(200);
  const raw = await api.text();
  const details = JSON.parse(raw) as MediaDetails;
  // The fixture carries GPS tags: only their presence is ever stored.
  expect(details.metadata.hasLocation).toBe(true);
  expect(raw).not.toMatch(COORDINATES);

  await openFiles(page, rootId);
  await showOnly(page, "photo");
  await fileRow(page, "photo.JPG").click();
  const panel = page.getByRole("region", {name: "File details"});
  const media = panel.getByRole("region", {name: "Photo details"});
  const value = (term: string) => media.locator("dl > div")
    .filter({has: page.locator("dt", {hasText: new RegExp(`^${term}$`)})})
    .locator("dd");
  await expect(value("Dimensions")).toHaveText("120 × 160 pixels");
  const captured = value("Captured");
  await expect(captured.locator("time")).toHaveAttribute("datetime", "2024-05-01T10:20:30Z");
  await expect(captured).toContainText("Exact: 2024-05-01T10:20:30");
  await expect(captured).toContainText("Recorded in the photo's EXIF metadata.");
  await expect(value("Camera")).toHaveText("Synthetic Camera Co. Model 7");
  expect(await panel.innerText()).not.toMatch(COORDINATES);
});

test("logout and a same-document account switch never bring earlier media back", async ({page}) => {
  await recordMediaElements(page);
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  const rootId = await aliceRootId(page);
  const photo = named(await mediaItems(page, rootId), "photo.JPG");
  await openAlicePhotos(page, rootId);
  const tile = photoTile(page, "photo.JPG");
  await tile.click();
  const shown = page.getByTestId("photo-stage").locator("img.photo-stage__image[data-loaded]");
  await expect(shown).toBeVisible({timeout: PREPARE_TIMEOUT_MS});
  await expectLoaded(shown);
  await page.keyboard.press("Escape");
  await expect(page).toHaveURL(new RegExp(`/files/${rootId}/photos$`));
  await expect(tile).toBeFocused();
  // Everything Alice's session showed for this photo, the preview included.
  const urls = photoUrls(await mediaDetails(page, photo.id));
  const [thumbnailUrl, previewUrl] = urls as [string, string];
  expect(await mediaShownSince(page, 0)).not.toEqual([]);

  // From the moment sign-out is asked for, no document of this tab may attach a media URL.
  const switched = await page.evaluate(() => Date.now());
  await page.getByRole("button", {name: "Sign out"}).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", {name: "Sign in"})).toBeVisible();
  await expectNoPrivateMedia(page, switched);
  // Anonymous: the same 404 for every URL.
  await expectAllRefused(page, urls);

  // The account switch stays in this document: Bob signs in on the form Alice's sign-out left,
  // so only the app's own cleanup (cache purge, namespace gating, history checks) protects her.
  expect(await submitLogin(page, "bob", requiredSecret("E2E_BOB_PASSWORD"))).toBe(200);
  await expect(page).toHaveURL(/\/roots$/);
  await expect(page.getByRole("heading", {name: "Bob files"})).toBeVisible();
  await expectNoPrivateMedia(page, switched);
  await expectAllRefused(page, urls);

  // This document's history: [Alice's roots, Alice's root, (Photos, replaced by the login and
  // then Bob's roots), Alice's viewer]. Every entry is re-checked and rendered for Bob.
  const unavailable = page.getByRole("heading", {name: "Root unavailable"});
  const bobFiles = page.getByRole("heading", {name: "Bob files"});
  const steps: {move: "back" | "forward"; url: RegExp; shows: Locator}[] = [
    {move: "back", url: new RegExp(`/files/${rootId}$`), shows: unavailable},
    {move: "back", url: /\/roots$/, shows: bobFiles},
    {move: "forward", url: new RegExp(`/files/${rootId}$`), shows: unavailable},
    {move: "forward", url: /\/roots$/, shows: bobFiles},
    {
      move: "forward", url: new RegExp(`/files/${rootId}/view/${photo.id}$`),
      shows: page.getByText("This root is not assigned to this account.", {exact: true}),
    },
  ];
  for (const step of steps) {
    const checked = page.waitForResponse((response) => new URL(response.url()).pathname === SESSION_PATH);
    await (step.move === "back" ? page.goBack() : page.goForward());
    expect((await checked).status()).toBe(200);
    await expect(page).toHaveURL(step.url);
    await expect(step.shows).toBeVisible();
    await expectNoPrivateMedia(page, switched);
  }

  // A fresh document: Alice's HTTP-cached thumbnail and preview must be revalidated with Bob's
  // cookie, and the server refuses them. (A document reuses an image it already loaded.)
  await page.goto(`/files/${rootId}/photos`);
  await expect(unavailable).toBeVisible();
  for (const url of [thumbnailUrl, previewUrl]) expect(await loadsInPage(page, url)).toBe(false);
  // The recorder spans every document of this tab.
  await expectNoPrivateMedia(page, switched);
});

test("a restored page hides earlier media when its session ended or changed elsewhere", async ({page}) => {
  await recordMediaElements(page);
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  const rootId = await aliceRootId(page);
  const photo = named(await mediaItems(page, rootId), "photo.JPG");
  await openAlicePhotos(page, rootId);
  const gridUrls = photoUrls(await mediaDetails(page, photo.id));

  // As in Phase 1: the session ends out of band (another tab) while Alice's grid is shown, then
  // the page is restored from the back/forward cache.
  const ended = await page.evaluate(() => Date.now());
  await apiSignOut(page);
  await restorePageFromHistoryCache(page);
  await expect(page.getByRole("heading", {name: "Sign in"})).toBeVisible();
  await expectNoPrivateMedia(page, ended);
  await expectAllRefused(page, gridUrls);

  // The switch variant, over Alice's open viewer: elsewhere her session ends and Bob's begins,
  // so the restored page finds a valid session of another namespace, not an error.
  expect(await submitLogin(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"))).toBe(200);
  await expect(page).toHaveURL(/\/roots$/);
  await openAlicePhotos(page, rootId);
  await photoTile(page, "photo.JPG").click();
  const shown = page.getByTestId("photo-stage").locator("img.photo-stage__image[data-loaded]");
  await expect(shown).toBeVisible({timeout: PREPARE_TIMEOUT_MS});
  await expectLoaded(shown);
  const viewerUrls = photoUrls(await mediaDetails(page, photo.id));
  const switched = await page.evaluate(() => Date.now());
  await apiSignOut(page);
  await apiSignIn(page, "bob", requiredSecret("E2E_BOB_PASSWORD"));
  await restorePageFromHistoryCache(page);
  // Bob's view of Alice's viewer route; a request the open viewer made between the two
  // sign-ins may instead have ended the page's session, which shows the sign-in form.
  await expect(
    page.getByText("This root is not assigned to this account.", {exact: true})
      .or(page.getByRole("heading", {name: "Sign in"})),
  ).toBeVisible();
  await expectNoPrivateMedia(page, switched);
  await expectAllRefused(page, viewerUrls);
});

test("revoking the grant turns every media URL into 404 and clears the open view", async ({page}) => {
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  const rootId = await aliceRootId(page);
  const photo = named(await mediaItems(page, rootId), "photo.JPG");
  // The earlier journeys of this browser built the preview, so all four URLs exist.
  const urls = photoUrls(await mediaDetails(page, photo.id));
  const [thumbnailUrl, previewUrl] = urls as [string, string];
  for (const url of urls) expect((await page.request.get(url)).status()).toBe(200);

  await page.goto(`/files/${rootId}/photos`);
  const tile = photoTile(page, "photo.JPG");
  await expectLoaded(tile.locator("img.media-thumbnail__image"));
  try {
    await setRootGrant(page, rootId, "0");
    await expectAllRefused(page, urls);
    // The open timeline's next private request closes it.
    await tile.click();
    await expect(page.getByRole("heading", {name: "Sign in"})).toBeVisible();
    await expect(page.locator(MEDIA_ELEMENTS)).toHaveCount(0);
    await expect(page.getByText(ALICE_TEXT)).toHaveCount(0);
    await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
    await expect(page.getByRole("heading", {name: "No roots assigned"})).toBeVisible();
    await expect(page.locator(MEDIA_ELEMENTS)).toHaveCount(0);
    await expectAllRefused(page, urls);
    // A fresh document (sign-in navigates) must revalidate the HTTP-cached copies, which the
    // server refuses. Browsers reuse an image within the document that loaded it.
    for (const url of [thumbnailUrl, previewUrl]) expect(await loadsInPage(page, url)).toBe(false);
  } finally {
    await setRootGrant(page, rootId, ALICE_PERMISSIONS);
  }
});

test("filters search all folders: .jpg finds the nested photo and the viewer follows the results", async ({page}) => {
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  const rootId = await aliceRootId(page);
  const beach = named(await mediaItems(page, rootId), "beach.jpg");
  await openFiles(page, rootId);

  await page.getByRole("button", {name: "Filters", exact: true}).click();
  const dialog = page.getByRole("dialog", {name: "Filter files"});
  await expect(dialog.getByRole("radio", {name: "All folders", exact: true})).toBeChecked();
  await dialog.getByLabel(".jpg", {exact: true}).check();
  const search = page.waitForResponse((response) =>
    ENTRIES_PATH.test(new URL(response.url()).pathname) && response.request().method() === "GET");
  await dialog.getByRole("button", {name: "Apply filters", exact: true}).click();
  const response = await search;
  expect(response.status()).toBe(200);
  const sent = new URL(response.url()).searchParams;
  expect(sent.get("scope")).toBe("root");
  expect(sent.has("parent")).toBe(false);

  // The photo two folders down is found, with its folder; the top-level matches name the root.
  const files = page.getByRole("list", {name: "Files"});
  await expect(files.getByRole("listitem")).toHaveCount(3);
  const nested = page.getByRole("button", {name: "Open photo beach.jpg in summer", exact: true});
  await expect(nested).toContainText("in summer");
  await expect(page.getByRole("button", {name: "Open photo photo.JPG in Alice files", exact: true}))
    .toBeVisible();
  // Scope and filters stay in memory, never in the address.
  await expect(page).toHaveURL(new RegExp(`/files/${rootId}$`));

  await nested.click();
  const viewer = page.getByRole("dialog");
  await expect(viewer.getByRole("heading", {level: 2})).toHaveText("beach.jpg");
  await expect(page).toHaveURL(new RegExp(`/files/${rootId}/view/${beach.id}$`));
  // Its neighbors are the search results (its own folder holds no other photo).
  await expect(viewer.getByText("1 of 3", {exact: true})).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/files/${rootId}$`));
  await expect(nested).toBeFocused();

  // This folder narrows the same filters to the root's own entries.
  const narrowed = page.waitForResponse((answer) =>
    ENTRIES_PATH.test(new URL(answer.url()).pathname) && answer.request().method() === "GET");
  await page.getByRole("group", {name: "Search in"}).getByRole("button", {name: "This folder"}).click();
  expect(new URL((await narrowed).url()).searchParams.has("scope")).toBe(false);
  await expect(files.getByRole("listitem")).toHaveCount(2);
  await expect(fileRow(page, "photo.JPG")).toBeVisible();
  await expect(page.getByText("beach.jpg", {exact: true})).toHaveCount(0);
});

test("name search finds text anywhere in names, in every folder, and opens a result in the viewer", async ({page}) => {
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  const rootId = await aliceRootId(page);
  const beach = named(await mediaItems(page, rootId), "beach.jpg");
  await openFiles(page, rootId);

  const box = page.getByRole("searchbox", {name: "Search names"});
  // Fewer than three characters never search.
  await box.fill(".j");
  await expect(page.getByRole("search").getByRole("status")).toHaveText("Type at least 3 letters or digits");
  const searched = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return ENTRIES_PATH.test(url.pathname) && response.request().method() === "GET" &&
      url.searchParams.get("filters")?.includes("contains") === true;
  });
  await box.fill(".JP");
  const response = await searched;
  expect(response.status()).toBe(200);
  const sent = new URL(response.url()).searchParams;
  expect(sent.get("scope")).toBe("root");
  expect(sent.has("parent")).toBe(false);
  expect(JSON.parse(sent.get("filters")!)).toEqual({v: 1, contains: ".JP"});

  // Case is ignored; the photo two folders down is found with its folder.
  const files = page.getByRole("list", {name: "Files"});
  await expect(files.getByRole("listitem")).toHaveCount(3);
  const nested = page.getByRole("button", {name: "Open photo beach.jpg in summer", exact: true});
  await expect(nested).toContainText("in summer");
  await expect(page.getByRole("button", {name: "Open photo photo.JPG in Alice files", exact: true}))
    .toBeVisible();
  await expect(page.getByRole("button", {name: 'Remove filter Name contains ".JP"', exact: true}))
    .toBeVisible();
  // The search stays in memory, never in the address.
  await expect(page).toHaveURL(new RegExp(`/files/${rootId}$`));

  await nested.click();
  const viewer = page.getByRole("dialog");
  await expect(viewer.getByRole("heading", {level: 2})).toHaveText("beach.jpg");
  await expect(page).toHaveURL(new RegExp(`/files/${rootId}/view/${beach.id}$`));
  // The viewer steps through the search results.
  await expect(viewer.getByText("1 of 3", {exact: true})).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/files/${rootId}$`));
  await expect(nested).toBeFocused();
  await expect(box).toHaveValue(".JP");

  // Escape in the box clears the search: the folder is listed again.
  const cleared = page.waitForResponse((answer) =>
    ENTRIES_PATH.test(new URL(answer.url()).pathname) && answer.request().method() === "GET");
  await box.focus();
  await page.keyboard.press("Escape");
  const listing = new URL((await cleared).url()).searchParams;
  expect(JSON.parse(listing.get("filters")!)).toEqual({v: 1});
  expect(listing.has("scope")).toBe(false);
  await expect(box).toHaveValue("");
  // Folders lead the unfiltered listing again.
  await expect(directoryRow(page, "albums")).toBeVisible();
});
