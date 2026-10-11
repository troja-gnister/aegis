import {QueryClient} from "@tanstack/react-query";
import {http, HttpResponse} from "msw";
import {afterEach, beforeEach, describe, expect, it} from "vitest";
import {server} from "../../test/server";
import {activateCacheNamespace, purgePrivateBrowserState} from "../auth/cache";
import {fetchEntryMedia, requestMediaDerivative} from "./api";
import {
  derivativeUrlFor,
  FIXTURE_FINGERPRINT,
  mediaDetailsBody,
  originalUrlFor,
} from "./test-fixtures";

const ENTRY_ID = "33333333-3333-4333-8333-333333333333";
const OTHER_ID = "44444444-4444-4444-8444-444444444444";
const NAMESPACE = "media-api-namespace";
const clients = new Set<QueryClient>();

beforeEach(async () => {
  const client = new QueryClient();
  clients.add(client);
  await activateCacheNamespace(client, NAMESPACE);
});

afterEach(async () => {
  await Promise.all([...clients].map((client) => purgePrivateBrowserState(client)));
  clients.clear();
});

function serveDetails(body: Record<string, unknown>) {
  server.use(http.get(`/api/v1/entries/${ENTRY_ID}/media`, () => HttpResponse.json(body)));
}

describe("fetchEntryMedia", () => {
  it("accepts a valid photo body and keeps capture metadata separate from file times", async () => {
    serveDetails(mediaDetailsBody(ENTRY_ID));

    const details = await fetchEntryMedia(ENTRY_ID, new AbortController().signal);

    expect(details).toMatchObject({
      entryId: ENTRY_ID,
      kind: "photo",
      fingerprint: FIXTURE_FINGERPRINT,
      metadata: {
        width: 4032,
        height: 3024,
        capturedAt: "2027-01-10T08:00:00+00:00",
        capturedAtSource: "exif",
        cameraModel: "Synthetic Optics Model 7",
        hasLocation: false,
      },
      derivatives: {
        thumbnail: {state: "ready", url: derivativeUrlFor(ENTRY_ID, "thumbnail")},
        preview: {state: "missing", url: null},
      },
      original: {
        contentType: "image/jpeg",
        inlineUrl: originalUrlFor(ENTRY_ID, "inline"),
        downloadUrl: originalUrlFor(ENTRY_ID, "attachment"),
        playbackType: null,
      },
      permissions: {preview: true, export: true},
    });
  });

  it("accepts a video body with a playback type and no photo-only derivatives", async () => {
    serveDetails(mediaDetailsBody(ENTRY_ID, {kind: "video"}));
    const details = await fetchEntryMedia(ENTRY_ID, new AbortController().signal);
    expect(details.kind).toBe("video");
    expect(details.metadata.durationMs).toBe(3_723_000);
    expect(details.original.playbackType).toBe("video/mp4; codecs=\"avc1.640028, mp4a.40.2\"");
    expect(details.derivatives.preview).toBeUndefined();
    expect(details.derivatives.transcode).toEqual({state: "missing", url: null});
  });

  it("drops a capture source that has no capture time", async () => {
    serveDetails(mediaDetailsBody(ENTRY_ID, {metadata: {capturedAt: null, capturedAtSource: "exif"}}));
    const details = await fetchEntryMedia(ENTRY_ID, new AbortController().signal);
    expect(details.metadata.capturedAt).toBeNull();
    expect(details.metadata.capturedAtSource).toBeNull();
  });

  it.each([
    ["another entry's ID", {entryId: OTHER_ID}],
    ["a malformed fingerprint", {fingerprint: "not-a-fingerprint"}],
    ["an unknown media kind", {kind: "audio"}],
    ["an extra private field", {sourcePath: "/private/archive/photo.jpg"}],
    ["a non-boolean location flag", {metadata: {hasLocation: "no"}}],
    ["an unknown metadata key", {metadata: {latitude: 51.5}}],
    ["an unknown derivative kind", {derivatives: {raw: {state: "ready", url: null}}}],
    ["a URL on a derivative that is not ready", {derivatives: {
      preview: {state: "pending", url: derivativeUrlFor(ENTRY_ID, "preview")},
    }}],
    ["a derivative URL for another entry", {derivatives: {
      thumbnail: {state: "ready", url: derivativeUrlFor(OTHER_ID, "thumbnail")},
    }}],
    ["a derivative URL for another kind", {derivatives: {
      preview: {state: "ready", url: derivativeUrlFor(ENTRY_ID, "thumbnail")},
    }}],
    ["a derivative URL without PREVIEW", {
      permissions: {preview: false, export: true},
      derivatives: {thumbnail: {state: "ready", url: derivativeUrlFor(ENTRY_ID, "thumbnail")}},
    }],
    ["a cross-origin download URL", {original: {downloadUrl: `https://elsewhere.test${originalUrlFor(ENTRY_ID, "attachment")}`}}],
    ["a protocol-relative download URL", {original: {downloadUrl: `//elsewhere.test/media/v1/originals/${ENTRY_ID}?d=attachment`}}],
    ["a script download URL", {original: {downloadUrl: "javascript:alert(1)"}}],
    ["a traversing download URL", {original: {downloadUrl: `/media/v1/originals/../../api/v1/entries/${ENTRY_ID}?d=attachment`}}],
    ["an inline URL as the download", {original: {downloadUrl: originalUrlFor(ENTRY_ID, "inline")}}],
    ["a download URL without EXPORT", {
      permissions: {preview: true, export: false},
      original: {downloadUrl: originalUrlFor(ENTRY_ID, "attachment")},
    }],
    ["an inline URL without PREVIEW", {
      permissions: {preview: false, export: true},
      derivatives: {},
      original: {inlineUrl: originalUrlFor(ENTRY_ID, "inline")},
    }],
    ["a malformed content type", {original: {contentType: "image/jpeg; x=<script>"}}],
    ["a derivative URL for another fingerprint", {derivatives: {
      thumbnail: {state: "ready", url: `/media/v1/derivatives/${ENTRY_ID}/thumbnail?f=${"b".repeat(64)}&n=x&e=4`},
    }}],
    ["a derivative URL without a namespace", {derivatives: {
      thumbnail: {state: "ready", url: `/media/v1/derivatives/${ENTRY_ID}/thumbnail?f=${FIXTURE_FINGERPRINT}&e=4`},
    }}],
    ["an original URL without an epoch", {original: {
      downloadUrl: `/media/v1/originals/${ENTRY_ID}?d=attachment&n=x`,
    }}],
  ])("rejects %s", async (_name, overrides) => {
    serveDetails(mediaDetailsBody(ENTRY_ID, overrides));
    await expect(fetchEntryMedia(ENTRY_ID, new AbortController().signal)).rejects.toMatchObject({status: 502});
  });

  it("accepts bounded Unicode camera text", async () => {
    serveDetails(mediaDetailsBody(ENTRY_ID, {
      metadata: {cameraMake: "Synthétique Optik®", cameraModel: "Modèle 7 — 光学"},
    }));
    const details = await fetchEntryMedia(ENTRY_ID, new AbortController().signal);
    expect(details.metadata.cameraMake).toBe("Synthétique Optik®");
    expect(details.metadata.cameraModel).toBe("Modèle 7 — 光学");
    expect(details.original.downloadUrl).toBe(originalUrlFor(ENTRY_ID, "attachment"));
  });

  it.each([
    ["a bidi override", {cameraModel: "Model \u202Egnp.exe"}, "cameraModel"],
    ["a bidi isolate", {cameraMake: "\u2066Optics\u2069"}, "cameraMake"],
    ["a control character", {cameraModel: "Model\u00077"}, "cameraModel"],
    ["a line separator", {cameraModel: "Model\u20287"}, "cameraModel"],
    ["a lone surrogate", {cameraModel: "Model \uD800"}, "cameraModel"],
    ["leading whitespace", {cameraMake: " Optics"}, "cameraMake"],
    ["more than 64 code points", {cameraModel: "光".repeat(65)}, "cameraModel"],
    ["a non-string camera", {cameraMake: 7}, "cameraMake"],
    ["a non-ASCII codec", {videoCodec: "h264é"}, "videoCodec"],
    ["a malformed container", {container: "mov mp4"}, "container"],
    ["a fractional dimension", {width: 10.5}, "width"],
    ["an orientation outside EXIF", {orientation: 9}, "orientation"],
    ["a negative duration", {durationMs: -1}, "durationMs"],
    ["a capture time without an offset", {capturedAt: "2027-01-10T08:00:00"}, "capturedAt"],
  ])("nulls only the field with %s and keeps the delivery URLs", async (_name, metadata, field) => {
    serveDetails(mediaDetailsBody(ENTRY_ID, {metadata}));
    const details = await fetchEntryMedia(ENTRY_ID, new AbortController().signal);
    expect(details.metadata[field as keyof typeof details.metadata]).toBeNull();
    expect(details.original.downloadUrl).toBe(originalUrlFor(ENTRY_ID, "attachment"));
    expect(details.derivatives.thumbnail?.url).toBe(derivativeUrlFor(ENTRY_ID, "thumbnail"));
    if (field !== "capturedAt") expect(details.metadata.capturedAt).toBe("2027-01-10T08:00:00+00:00");
    else expect(details.metadata.capturedAtSource).toBeNull();
  });

  it("accepts the backend's codec and comma-separated container names", async () => {
    serveDetails(mediaDetailsBody(ENTRY_ID, {kind: "video", metadata: {
      videoCodec: "avc1.640028", audioCodec: "mp4a.40.2", container: "mov,mp4,m4a,3gp,3g2,mj2",
    }}));
    const details = await fetchEntryMedia(ENTRY_ID, new AbortController().signal);
    expect(details.metadata).toMatchObject({
      videoCodec: "avc1.640028", audioCodec: "mp4a.40.2", container: "mov,mp4,m4a,3gp,3g2,mj2",
    });
  });

  it("surfaces a non-media entry as a 404 problem", async () => {
    server.use(http.get(`/api/v1/entries/${ENTRY_ID}/media`, () =>
      HttpResponse.json({type: "catalog_not_found", title: "Catalog item not found"}, {status: 404}),
    ));
    await expect(fetchEntryMedia(ENTRY_ID, new AbortController().signal)).rejects.toMatchObject({status: 404});
  });

  it("refuses to start for a malformed entry ID", async () => {
    await expect(fetchEntryMedia("../roots", new AbortController().signal)).rejects.toMatchObject({status: 502});
  });
});

describe("requestMediaDerivative", () => {
  it("posts the derivative kind with the shared CSRF token", async () => {
    let seen: {token: string | null; body: unknown} | undefined;
    server.use(http.post(`/api/v1/entries/${ENTRY_ID}/media/requests`, async ({request}) => {
      seen = {token: request.headers.get("X-CSRFToken"), body: await request.json()};
      return HttpResponse.json({state: "pending", kind: "preview"}, {status: 202});
    }));

    await expect(requestMediaDerivative(ENTRY_ID, "preview")).resolves.toEqual({state: "pending", kind: "preview"});
    expect(seen).toEqual({token: "csrf-test-token", body: {kind: "preview"}});
  });

  it("retries one CSRF rejection with a replacement token", async () => {
    const tokens: (string | null)[] = [];
    let csrfRequests = 0;
    server.use(
      http.get("/api/v1/auth/csrf", () => {
        csrfRequests += 1;
        return HttpResponse.json({csrfToken: csrfRequests === 1 ? "first-token" : "replacement-token"});
      }),
      http.post(`/api/v1/entries/${ENTRY_ID}/media/requests`, ({request}) => {
        tokens.push(request.headers.get("X-CSRFToken"));
        return tokens.length === 1
          ? HttpResponse.json({type: "csrf_failed", title: "Request verification failed"}, {status: 403})
          : HttpResponse.json({state: "ready", kind: "transcode"}, {status: 202});
      }),
    );

    await expect(requestMediaDerivative(ENTRY_ID, "transcode")).resolves.toEqual({state: "ready", kind: "transcode"});
    expect(tokens).toEqual(["first-token", "replacement-token"]);
  });

  it("surfaces an unsupported or quarantined asset as a 409 problem", async () => {
    server.use(http.post(`/api/v1/entries/${ENTRY_ID}/media/requests`, () =>
      HttpResponse.json({type: "media_unsupported", title: "Media cannot be prepared"}, {status: 409}),
    ));
    await expect(requestMediaDerivative(ENTRY_ID, "preview")).rejects.toMatchObject({status: 409});
  });

  it.each([
    ["a different kind", {state: "pending", kind: "transcode"}],
    ["an unknown state", {state: "queued", kind: "preview"}],
    ["an extra field", {state: "pending", kind: "preview", jobId: "private"}],
  ])("rejects a 202 body with %s", async (_name, body) => {
    server.use(http.post(`/api/v1/entries/${ENTRY_ID}/media/requests`, () =>
      HttpResponse.json(body, {status: 202}),
    ));
    await expect(requestMediaDerivative(ENTRY_ID, "preview")).rejects.toMatchObject({status: 502});
  });
});
