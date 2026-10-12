# Media viewing for read-only roots: design

**Date:** October 11, 2026

**Status:** Part 4 of the Fedora Server continuation. Routine design decisions are delegated to the controller (October 10, 2026).

**Parent:**
- [HANDOFF.md](../../../HANDOFF.md);
- [Phase 2 delivery design](2026-09-14-phase-2-v1-delivery-design.md), §4–§6;
- [platform design](2026-08-31-aegis-platform-rewrite-design.md), sections on delivery, the volumes table, assets, media and the viewers;
- [part 2 deployment design](2026-10-10-podman-lan-deployment-design.md).

**Baseline:** `main` at `c60a2f4` (part 2 deployed; not yet pushed).

## 1. Outcome and boundary

A user with a grant on a read-only root can:
- see thumbnails for photos and videos in the file browser and in a new Photos timeline;
- open a full-screen photo viewer (swipe, zoom) and a video player with seeking;
- download originals.

Everything is authorized per request, served privately, and works on phones. Thumbnails and metadata build in the background for large libraries without touching the originals.

### Roadmap amendment

The delivery design orders 2B (managed files) before 2C (photos and videos). This part delivers, for read-only roots only:
- 2A's authorized downloads and ranges (FILE-005);
- the 2C media subset (MEDIA-001 to MEDIA-004): thumbnails, posters, metadata, timeline and viewers.

2C's integration with 2B publication, documents (2D), albums and search (2E), the PWA install flow (UX-003) and models (2F) are out of scope. Later packages extend this contract and do not replace it.

### Out of scope

- Uploads and any write to roots.
- RAW camera formats, documents and audio playback.
- Albums, tags and semantic search.
- Storing GPS coordinates. Only a "has location" flag is kept, so locations need a later opt-in.
- A service worker or offline access.

## 2. Context (measured October 11)

- **Roles:**
  - The media role is a stub that polls the generic job queue, and `aegis_media` has no catalog access.
  - The gateway already has `internal` aliases `/__aegis_roots/` and `/__aegis_derivatives/`, but nothing emits `X-Accel-Redirect`.
  - Root binds reach the gateway, indexer, media and operations; web has none.
- **Catalog:**
  - `CatalogEntry` carries `type_hint` (the lowercase extension), `size`, `mtime_ns`, `ctime_ns`, `device`, `inode` and a parent chain of raw names. It has no MIME detection and no content hash.
  - `source_revision` also changes when a sibling changes, or on a ctime-only change.
- **Permissions:** only BROWSE is enforced. PREVIEW and EXPORT exist, unused.
- **Gateway headers:** `/api/` forces `Cache-Control: private, no-store`. CSP is `default-src 'self'`, so there is no `blob:`.
- **Tooling:** no image or video tools exist in any image or the lockfile, and the e2e "media" fixtures are placeholder text.

## 3. Decisions

### M1. Derivative identity is a source fingerprint, not `source_revision`

- **Fingerprint:** `sha256(root_id, entry_id, device, inode, size, mtime_ns)`.
- **Derivative key:** `sha256(fingerprint, processor_version, kind, params)`, stored at `<derivatives>/<k[0:2]>/<k[2:4]>/<k>.<ext>`.

When a file's fingerprint changes, its derivatives go stale and are regenerated. Sibling changes and ctime-only changes do not invalidate them (platform design, asset revision). A strong content hash is not computed in this part.

### M2. A credential-free, network-free processor service

Decoding hostile media happens only in a new `media-processor` service, using a new `aegis-media` image (the backend runtime plus Debian's `ffmpeg`, with Pillow and pillow-heif from the locked Python environment).

The processor:
- has no network (`network_mode: none` / `Network=none`) and no database or secret material;
- mounts the roots read-only and one scratch volume read-write. It gets the same group mapping and SELinux reader type as the other original readers;
- runs one tool child per job with argument arrays and a scrubbed environment, under `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE` and `RLIMIT_NOFILE`, a wall-clock timeout and a low CPU priority;
- confines that child further, after the security review:
  - the loop opens the input file and passes the child only the open descriptor;
  - Landlock restricts the child to reading system libraries and writing its own fresh output directory, and denies network access where the kernel supports that;
  - the processor refuses to start without Landlock;
  - the loop is a subreaper and kills every leftover descendant after each job;
- before decoding, checks that the input matches the expected fingerprint (device, inode, size, mtime). If it doesn't, it reports "changed".

The processor and the media worker communicate only through the scratch volume:
1. The worker writes a job file atomically into `inbox/`.
2. The processor claims it by renaming it into `work/`.
3. It writes the outputs and a bounded result file into `done/<job>/`.
4. The worker validates the outputs (size, decodability markers, dimensions), moves them into derivatives, records them, and deletes the scratch files.

The worker never decodes media. The processor never reaches the database. Poison inputs are quarantined after bounded retries: the asset is marked `quarantined`, the attempts are kept, and no retry loop runs.

### M3. Media assets are root maintenance; interactive requests are jobs

The media worker discovers work from the catalog, like the indexer's system-driven model:
- it keyset-scans present file entries whose extension is on the media allowlist;
- it upserts a `MediaAsset` per entry, keyed by fingerprint;
- it leases batches with fenced leases.

The background pipeline runs **probe**, then **thumbnail** (photos), or **poster** (videos), in two lanes:
- **interactive:** preview and transcode requests;
- **background:** backfill.

The interactive lane always goes first. A new asset's background work is one `backfill` processor job that probes and renders its thumbnail (or poster pair) from one opened file; the worker records the metadata, the timeline key and the derivatives in one transaction. Background claims take the newest files first (by modification time, per root, through a partial index), so the top of the Photos timeline fills early. The worker keeps up to twice the processor concurrency in flight, plus one slot for interactive work.

Interactive requests (a viewer preview, a video transcode) are actor-bound `Job`s through the existing operation queue. Their authorization snapshot is re-checked before execution. The database grants for `aegis_media` grow explicitly:
- SELECT on the catalog entry columns it needs, and on `roots_root.slot_id`;
- full DML on the new media tables only.

Web gets SELECT on the media tables and INSERT through the existing operation services. Every addition goes into the privilege allowlists.

### M4. Formats, sizes and budgets (defaults)

| Category | Formats or values |
|---|---|
| Photos | jpg, jpeg, png, webp, gif, heic, heif, avif, bmp, tif, tiff |
| Videos | mp4, m4v, mov, webm, mkv, avi, 3gp, mts, m2ts |
| Thumbnail | Fits within 400×400, WebP quality 75, EXIF orientation applied. Built for every asset during backfill. |
| Viewer preview | Fits within 2048×2048, WebP quality 82. Built on demand and cached. |
| Video poster | A frame at 1 s (or the first frame), produced at thumbnail and preview sizes |
| Transcode | H.264 High (≤1080p), AAC, MP4 with faststart. Built on demand, only when the browser cannot play the original. |
| Budget | Thumbnails and posters are kept. Previews and transcodes are evicted least-recently-used above `AEGIS_DERIVATIVES_CACHE_BUDGET_GB` (default 20). |
| Concurrency | Processor children, `AEGIS_MEDIA_PROCESSOR_CONCURRENCY` (default 4), at nice 10, within 4 CPUs and 13 GiB (4 × the 3 GiB transcode budget + 1 GiB) |

Pillow's decompression-bomb limit is enforced, and ffmpeg runs with `-nostdin -protocol_whitelist file,pipe`.

### M5. Metadata

The probe records:
- width and height as displayed (EXIF orientation or video rotation applied), plus the orientation itself for information;
- `captured_at`, with its source (`exif`, `container`, or none);
- camera make and model;
- duration, video and audio codecs, and container (videos);
- `has_location`.

Coordinates are discarded in the processor and never stored. The timeline sort key is `captured_at`, falling back to `mtime`. The UI always says which one it shows; a fallback date is never labeled as a capture date (delivery design §5).

### M6. Delivery through Django authorization and nginx bytes

A new gateway location, `/media/`, proxies to web without forcing `no-store`. Django sets the caching per response.

| Endpoint | Permission | Response |
|---|---|---|
| `GET /media/v1/derivatives/{entry}/{kind}?f=<fingerprint>&n=<cache namespace>&e=<root epoch>` | PREVIEW | Strong ETag (the derivative key), `Cache-Control: private, no-cache`, `Vary: Cookie`. Answers `304` itself. On `200`, `X-Accel-Redirect` to `/__aegis_derivatives/...` with the content type set by Django. |
| `GET /media/v1/originals/{entry}?d=inline\|attachment&n=…&e=…` | `inline`: PREVIEW, and only for allowlisted browser-native types. `attachment`: EXPORT. | `Cache-Control: private, no-store`, `X-Accel-Redirect` to `/__aegis_roots/<slot>/<percent-encoded raw path>`, safe RFC 6266 `Content-Disposition`, content type from the extension allowlist (never file-sniffed), `nosniff`. Every other type is attachment-only. |
| `GET /api/v1/entries/{id}/media` | — | Metadata, derivative states, delivery URLs. Under `/api/`, `no-store`. |
| `POST /api/v1/entries/{id}/media/requests` | PREVIEW | Body `{kind: preview\|transcode}`, with CSRF. Queues an interactive job and answers 202 with its state. |
| `GET /api/v1/roots/{root}/timeline` | — | Keyset (sort key descending, then id), signed cursors bound like the catalog cursors, filters `kind=photo\|video` and a year. |

Delivery rules:
- nginx serves ranges (`max_ranges 1`) and conditional requests on the redirected file. The internal locations keep `disable_symlinks on`.
- The catalog's entry list and details gain a compact `media` object: kind, thumbnail state, sort date and duration.
- The URLs carry the cache namespace and root epoch, so a browser cache can never serve one user's derivative to another session or after revocation. Logout and account switch already purge the private state; tracked object URLs are not needed, because images and videos load from same-origin URLs.

### M7. Frontend

- **File browser:**
  - Fix extension classification: `type_hint` is an extension.
  - Rows show lazy thumbnails when they are ready, and a type icon otherwise, so pending, failed and unsupported are distinct.
  - The details panel shows metadata, plus Open and Download (shown only when permitted).
- **Photos:**
  - A per-root route `/files/:rootId/photos` holds a virtualized grid (3 columns on phones, more on wider screens), grouped by month, with keyset paging (`maxPages` bound), bounded prefetch and cancellation.
  - A kind filter: All, Photos, Videos.
- **Viewer:**
  - Route state over the current context: a directory listing or the timeline.
  - Photos: full screen, swipe left and right, pinch and double-tap zoom, keyboard arrows and Escape. It shows the preview derivative, requesting it when missing and showing the thumbnail meanwhile, and prefetches ±1 neighbor.
  - Videos: a `<video>` element on the original when the browser reports it can play the codec (`canPlayType` with the probed codec string). Otherwise it requests a transcode and shows "Preparing playback…" with progress polling.
  - Focus management, and an accessible name on every control.
- **Shell:** navigation between Files and Photos for the current root.

### M8. Deployment

- **Images:**
  - Compose adds `media-processor` (image `aegis-media`, `network_mode: none`, read-only, cap_drop ALL, no-new-privileges, a resource-bounded tmpfs, no secrets) and a `media-scratch` volume.
  - The backend image pre-creates the derivatives, scratch and quarantine mount points, owned by the runtime UID.
  - CI publishes five images. The installer pulls five.
  - Quadlet gains `media-processor.container` and `media-scratch.volume`, under the parity rules.
- **`--derivatives-dir <path>` for the installer:**
  - The `derivatives` volume becomes a bind of an operator-created directory (`Type=none`, `Device=<path>`, `Options=bind`), so a large library's thumbnails can live on another disk.
  - The installer validates the directory: it exists, belongs to the operator, has no other content, sits outside every root and every installer directory, and is not on tmpfs.
  - It prepares it the same way it prepares files for mapped containers (container uid, the operator's group, `container_file_t`, mode 2770).
  - This is a parity-allowed difference.

### M8a. One SELinux level per install

Podman gives each container a random MCS category pair, and files a container creates carry its pair. Containers that share volumes therefore could not read each other's files:
- media and the processor (scratch);
- media and the gateway (derivatives);
- web and operations (staging).

The fix:
- **Quadlet:** every Aegis container in an install runs at one level, `s0:cX,cY`, chosen at random at `init` and recorded. Aegis containers can exchange files while staying isolated from every other container on the host.
- **Podman compose (dev/test):** the overlay uses one fixed, documented level.
- **Verification:** the deployment harness checks the exchange.

### M9. Image hygiene

Published images must never contain host data or media.
- A tracked test checks two things:
  - each Dockerfile's `COPY`/`ADD` sources are within an allowlist of tracked source paths;
  - `.dockerignore` covers local-only material at any depth: env files, secrets, mount configs and manifests, generated overrides, the ignored notes directory, and test outputs.
- After publishing, an untracked audit scans the published images' files for host-specific values and media-like content. Its search list is private, so it lives only in the ignored notes.

## 4. Verification (run at the end of the phase, together with part 2)

The user ordered that tests be written throughout but run only at the end of the phase.

- **Backend:**
  - discovery, fingerprints and leases;
  - lane priority;
  - processor protocol validation (malformed result, oversize output, a changed input);
  - privilege tests per role;
  - delivery headers and permissions (PREVIEW, EXPORT, inline allowlist);
  - namespace and epoch rejection;
  - percent-encoding of non-UTF-8, `%`, `?` and space in raw names;
  - timeline keyset.
- **Processor:** real fixtures generated at test time (JPEG with EXIF orientation and GPS, PNG, HEIC, an MP4 in H.264, an MOV in HEVC, a corrupt file). Check bounds, coordinates stripped, and quarantine.
- **Gateway:** `X-Accel-Redirect` header passthrough, ranges and 304, and that internal aliases stay unreachable.
- **e2e (mobile Chromium and WebKit):**
  - a thumbnail appears;
  - the viewer swipes and zooms;
  - video plays and seeks;
  - download works;
  - privacy journeys: logout, account switch and revocation never show a previously seen thumbnail or original.
- **Deployment:** compose and Quadlet parity for the new service and volume, the image-hygiene test, and the D12 harness extended with a processor check.
- **Performance:**
  - backfill throughput and peak memory on the real root (provisional);
  - a mobile grid scroll with at most 80 rendered tiles;
  - viewer open-to-image time.

## 5. Safety

All earlier safety rules apply:
- originals stay read-only everywhere;
- no relabel or chown of originals;
- exact-identity cleanup only;
- no host-specific values in tracked files;
- the user's existing services stay untouched.

The derivatives directory is a new, dedicated directory. Nothing else on that disk is touched.

## 6. Order

1. Spec and plan.
2. Image hygiene test and `.dockerignore`.
3. Media data model, grants and discovery.
4. The processor service, image and protocol.
5. The worker pipeline (lanes, validation, publishing, eviction).
6. Delivery endpoints and the gateway.
7. Interactive requests.
8. Timeline API.
9. The frontend (files thumbnails, details, Photos, viewer).
10. Deployment changes: compose, Quadlet, installer `--derivatives-dir`, CI's five images.
11. e2e journeys and fixtures.
12. End-of-phase verification, for parts 2 and 4.
13. Server upgrade and backfill measurement, user acceptance, then docs.
