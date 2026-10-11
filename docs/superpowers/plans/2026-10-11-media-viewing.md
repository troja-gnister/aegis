# Media viewing for read-only roots: implementation plan

**Spec:** [2026-10-11-media-viewing-design.md](../specs/2026-10-11-media-viewing-design.md). Decisions are cited as M1–M9; read the spec first.

## Phase rules

These apply to every task and come from the user's instructions of October 11, 2026.

- **Tests:**
  - Write every test alongside the code, at the depth this project requires.
  - Do NOT run any test suite during the phase (pytest, Vitest, Playwright, `make verify*`, the D12 harness).
  - Static checks are allowed and expected: `ruff`, `mypy`, `tsc`/typecheck and `eslint`. All tests run once, at the end of the phase (Task 13).
- **Git:**
  - The root session commits after each task's review.
  - Nobody pushes until Task 13.
- **Workflow:**
  - Implementers do not commit, push, dispatch agents, or start long-lived containers.
  - Reviews are static (reading code and running static checks).
- **Host safety and hygiene:** all HANDOFF and part 2 rules apply. In particular, no host-specific values may appear in tracked files.

## API contract

This is the shared contract between backend and frontend; tasks 5–10 must match it.

### Entry `media` field

Entry summaries (from the entries list, entry details and the timeline) gain `media`, which is `null` for non-media entries:

```json
{"kind": "photo" | "video",
 "thumbnail": "pending" | "ready" | "failed" | "unsupported" | "quarantined",
 "sortDate": "<RFC 3339 UTC>", "sortDateSource": "captured" | "modified",
 "width": 4032 | null, "height": 3024 | null, "durationMs": 12345 | null,
 "thumbnailUrl": "/media/v1/derivatives/<entry>/thumbnail?f=<fp>&n=<ns>&e=<epoch>" | null}
```

`thumbnailUrl` is present only when `thumbnail` is `"ready"` and the caller has PREVIEW.

### `GET /api/v1/entries/{id}/media`

Requires BROWSE; returns 404 for non-media entries. Response:

```json
{"entryId": "<uuid>", "kind": "photo" | "video", "fingerprint": "<64 hex>",
 "metadata": {"width": int|null, "height": int|null, "orientation": int|null,  // display dimensions: orientation/rotation already applied
              "capturedAt": str|null, "capturedAtSource": "exif"|"container"|null,
              "cameraMake": str|null, "cameraModel": str|null, "durationMs": int|null,
              "videoCodec": str|null, "audioCodec": str|null, "container": str|null,
              "hasLocation": bool},
 "derivatives": {"<kind>": {"state": "missing"|"pending"|"ready"|"failed"|"unsupported"|"quarantined",
                            "url": str|null}},
 "original": {"contentType": str, "inlineUrl": str|null, "downloadUrl": str|null,
              "playbackType": "video/mp4; codecs=\"avc1.640028, mp4a.40.2\"" | null},
 "permissions": {"preview": bool, "export": bool}}
```

- Derivative kinds are `thumbnail`, `preview`, `poster` and `transcode`. Videos have `poster` and `transcode`; photos have `preview`.
- A URL is non-null only when the state is `ready` and the permission allows it.

### `POST /api/v1/entries/{id}/media/requests`

Requires CSRF and PREVIEW. Body: `{"kind": "preview"|"transcode"}`.

- Responds 202 with `{"state": "pending"|"ready", "kind": ...}`.
- It is idempotent while a request is pending or the derivative is ready.
- Responds 409 if the asset is `unsupported` or `quarantined`.

### `GET /api/v1/roots/{root}/timeline`

Parameters: `kind=all|photo|video`, `year=YYYY` (optional), `cursor`, `limit` (≤ 120, default 60).

- Response: `{"items": [<entry summary + parentId>], "nextCursor": str|null, "contractVersion": 1}`.
- Ordering: `sortDate` descending, then entry id descending.
- Cursors are signed and bound like the catalog cursors, including user, namespace, epoch, root, filters and limit, plus a 900 s expiry.

### Media delivery

These live under `/media/v1/` (gateway location `/media/`, which does not force `no-store`):

| Endpoint | Rules |
|---|---|
| `GET /media/v1/derivatives/{entry}/{kind}?f=&n=&e=` | Requires PREVIEW. Strong ETag; answers 304 itself; otherwise `X-Accel-Redirect: /__aegis_derivatives/<aa>/<bb>/<key>.<ext>`. Headers: `Cache-Control: private, no-cache`, `Vary: Cookie`, exact `Content-Type`. Returns 404 when not ready or stale. A wrong `f`, `n` or `e` returns 404, never a different derivative. |
| `GET /media/v1/originals/{entry}?d=inline\|attachment&n=&e=` | `inline` requires PREVIEW and an allowlisted type (jpg, jpeg, png, webp, gif, avif, mp4, m4v, webm, mov); `attachment` requires EXPORT. `X-Accel-Redirect` to `/__aegis_roots/<slot>/<percent-encoded path>`. Headers: `Cache-Control: private, no-store`, `Content-Disposition` per RFC 6266 (ASCII fallback plus `filename*`), `X-Content-Type-Options: nosniff`, and `Content-Type` from the extension allowlist (`application/octet-stream` for attachments of other types). |

---

## Task 1: Image hygiene (M9)

**Files:**
- `.dockerignore`;
- new `tests/deployment/test_image_hygiene.py`.

**Requirements:**
- `.dockerignore` covers, at any depth:
  - `**/.env` and `**/.env.*` (keeping `!**/.env.example`);
  - `.superpowers`, `deploy/secrets`, `deploy/mounts.*.toml`, `deploy/mounts.manifest.json`, `deploy/mounts.gateway.attestation` and `compose.mounts.generated.yaml`;
  - `**/test-results`, `**/playwright-report`, `**/blob-report`, `**/coverage` and `*.log`.

  The existing entries stay.
- The test:
  - parses every `docker/*.Dockerfile`;
  - requires that each `COPY`/`ADD` without `--from` copies only from an allowlist of tracked paths: `pyproject.toml`, `uv.lock`, `backend/manage.py`, `backend/aegis`, `backend/aegis_apps`, `backend/aegisctl`, `backend/aegis_media_processor` (Task 3), `deploy/podman`, `deploy/caddy/...`, `deploy/nginx/...`, `deploy/postgres/...`, `frontend/` (the gateway build) and `frontend/package*.json`;
  - requires that `ADD` is never used with a URL;
  - checks that `.dockerignore` contains each required pattern;
  - checks that every allowlisted path is tracked by git (`git ls-files`).
- Static checks: ruff.

## Task 2: Media data model, grants, configuration (M1, M3, M4)

**Files:**
- a new app `backend/aegis_apps/media/`: models, enums, fingerprint and keys, config, admin read-only views, migrations;
- `common/database_privileges.py` (allowlists and grants);
- `operations/enums.py` plus a migration (JobKind `media.preview`, `media.transcode`);
- settings registration;
- tests under `backend/tests/{unit,integration}/media/`, plus privilege tests extended in their existing files.

**Models:**
- `MediaAsset`:
  - entry (one-to-one with the catalog entry, protected) and root;
  - fingerprint fields (device, inode, size, mtime_ns) and `fingerprint` (64 hex);
  - `kind` (photo or video);
  - probe, thumbnail and poster states, using the enums from the contract;
  - attempts, `next_attempt_at`, `lease_owner`, `lease_expires_at`, `lane` (interactive or background), `processor_version` and `safe_error_code`;
  - the M5 metadata columns;
  - `sort_at` and `sort_source`;
  - `created_at` and `updated_at`.
  - Indexes: `(root, sort_at desc, entry desc)`, `(kind)`, and the leasing indexes on state and `next_attempt_at`.
  - CHECK constraints on every enum and on the bounded string lengths.
- `MediaDerivative`:
  - asset, kind, `params_digest`, `key` (64 hex, unique), extension, bytes, width and height;
  - state;
  - `created_at` and `last_access_at`;
  - `evictable` (true for preview and transcode).

**Functions:**
- `fingerprint(root_id, entry_id, device, inode, size, mtime_ns)`, `derivative_key(...)` and `relative_path(key, ext)`, exactly as M1 defines them;
- the extension allowlists and the kind mapping (M4);
- the content-type maps.

**Configuration:**
- `AEGIS_DERIVATIVES_CACHE_BUDGET_GB` (default 20, range 1–10000);
- `AEGIS_MEDIA_PROCESSOR_CONCURRENCY` (default 2, range 1–16);
- `AEGIS_MEDIA_DISCOVERY_BATCH` (default 500);
- the probe, thumbnail, preview and transcode timeouts and memory limits, as bounded integers;
- fail-closed parsing, following `operations/config.py`.

**Grants (M3):**
- `aegis_media` gets:
  - SELECT on `catalog_catalogentry` (id, root_id, source_parent_id, raw_name, kind, type_hint, source_state, size, mtime_ns, device, inode);
  - SELECT on `roots_root.slot_id`;
  - SELECT, INSERT and UPDATE on the media tables, with DELETE on `media_mediaderivative` only.
- `aegis_web` gets SELECT on the media tables.
- Every addition goes into `MANAGED_TABLE_COLUMNS` and the privilege tests. No other role changes.

**Tests (written, not run):**
- fingerprint and key vectors;
- model constraints;
- config bounds;
- per-role privilege tests, including denials.

**Static checks:** ruff and mypy.

## Task 3: Processor service, image and protocol (M2, M4, M5)

**Files:**
- a new package `backend/aegis_media_processor/`, standard library plus Pillow and pillow-heif; no Django import:
  - `protocol.py`: job and result schemas, strict JSON validation and size bounds, shared with the worker by import;
  - `loop.py`: the `inbox`, `work` and `done` loop, with atomic claims by rename, a startup sweep of stale `work/` claims, and graceful SIGTERM;
  - `sandbox.py`: child spawn with rlimits, timeout, scrubbed environment, `nice` and closed file descriptors;
  - `ops/probe.py`, `ops/image.py`, `ops/video.py`;
- `pyproject.toml` and `uv.lock`: add Pillow and pillow-heif, pinned. Run `uv lock` and record the versions;
- `docker/media.Dockerfile`: `FROM` the backend runtime stage pattern with the same base digest, plus `apt-get install --no-install-recommends ffmpeg` with the apt lists removed and the installed ffmpeg version written to a label. Runs as user 10001. Its entrypoint is `python -m aegis_media_processor`;
- the backend Dockerfile copies the new package;
- `deploy/images.lock` is unchanged, because the bases are unchanged;
- tests in `backend/tests/unit/media_processor/` and `tests/deployment/` for the image.

**Protocol:**
- **Job:** id, op (probe, thumbnail, preview, poster or transcode), input (`slot`, relative path bytes base64, expected device, inode, size and mtime_ns), params and limits.
- **Result:** status (ok, unsupported, changed, failed or timeout), `safe_error_code`, metadata for a probe, and outputs (name, bytes, width, height, format).
- **Bounds:**
  - outputs only inside the job's `done/<id>/`;
  - result JSON ≤ 64 KiB;
  - no symlinks;
  - every field validated on both sides.

**Operations:**
- **Probe:**
  - images: Pillow open plus `getexif()` (orientation, DateTimeOriginal with offset, make, model, GPS presence only) and size;
  - videos: `ffprobe -v error -print_format json -show_format -show_streams` on the path, with a timeout, mapping duration, codecs, container, rotation and `creation_time`;
  - coordinates are never output.
- **Thumbnail and preview:** Pillow with `draft()` for JPEG; HEIC through pillow-heif; `ImageOps.exif_transpose`; fit-within resize; WebP save. Animated GIFs use their first frame.
- **Poster:** ffmpeg extracts a frame at 1 s (falling back to the first frame) to a temporary PNG in the job directory, then Pillow resizes it to the thumbnail and preview sizes.
- **Transcode:** `ffmpeg -nostdin -protocol_whitelist file,pipe -i <in> -map 0:v:0 -map 0:a:0? -c:v libx264 -preset veryfast -profile:v high -vf "scale='min(1920,iw)':-2" -c:a aac -b:a 160k -movflags +faststart <out>`, with a timeout proportional to duration and capped.

**Tests (written):**
- protocol validation, both valid and hostile: path traversal, oversize, extra fields, wrong types;
- sandbox limits;
- ops against fixtures generated in the test: Pillow writes JPEG with EXIF orientation 6 and GPS, PNG and HEIC; ffmpeg writes MP4 and MOV in the media image test;
- that coordinates are stripped;
- fingerprint mismatch gives `changed`;
- a corrupt input gives `failed`.

**Static checks:** ruff and mypy (extend mypy to the new package).

## Task 4: Worker pipeline (M2, M3, M4)

**Files:**
- `backend/aegis_apps/media/{discovery,pipeline,publish,eviction}.py`;
- the media branch in `run_role.py`;
- heartbeat metrics;
- tests.

**Behavior:**
- **Discovery:**
  - keyset scan of present file entries with allowlisted extensions, per active root;
  - upsert assets with the fingerprint;
  - a stale fingerprint resets the states and marks the old derivatives stale (files removed by eviction, never in place).
- **Leasing:** fenced leases on assets, on the same pattern as the indexer's leases. The interactive lane comes first, and its jobs come from the operation queue (`media.preview` and `media.transcode`, with the authorization snapshot re-checked).
- **Dispatch:**
  - writes job files atomically (`O_EXCL` and rename) into `scratch/inbox`;
  - keeps at most the processor concurrency plus one in flight;
  - polls `done/`;
  - validates the result against the protocol;
  - checks each output's bytes, image magic (RIFF/WEBP, MP4 `ftyp`) and dimension bounds.
- **Publishing:**
  - moves outputs to `<derivatives>/<aa>/<bb>/<key>.<ext>` (create the directories at 0750; files at 0640, group-readable for the gateway);
  - inserts the derivative row;
  - updates the asset;
  - deletes the job directory.
- **Retries:** exponential backoff. After 3 failures the asset is quarantined. `changed` triggers rediscovery.
- **Eviction:** when evictable bytes exceed the budget, delete the least recently used evictable derivatives (row first, then the file). Never delete thumbnails or posters.
- **Heartbeat metrics:** queue depths, in-flight jobs, the last error code, and backfill progress counts.
- **Tests (written):** discovery idempotency; fingerprint change; lane order; validation rejection paths; publish modes; retry and quarantine; eviction order and budget; that heartbeat metrics carry no private names.

**Static checks:** ruff and mypy.

## Task 5: Delivery endpoints and gateway (M6)

**Files:**
- `backend/aegis_apps/media/{delivery,urls}.py`, wired into `backend/aegis/urls.py`;
- `deploy/nginx/aegis-server.conf`: a `location /media/` proxied to web exactly like `/api/` except for the no-store header, plus `max_ranges 1;` and `sendfile on;` in the internal locations;
- authorization helpers that enforce PREVIEW and EXPORT the same way `browse_context` handles BROWSE (FOR SHARE locks, epoch, binding);
- percent-encoding of the raw path;
- tests: backend unit and integration, and gateway tests extending the echo-upstream harness to answer with `X-Accel-Redirect`.

**Requirements:** as in the contract.
- **The redirect path:** built from the parent chain's raw names, each byte outside `[A-Za-z0-9._~-]` percent-encoded. The result is checked against a regex before emission, with no `..` segments.
- **Conditional and caching headers:** ETag equals the derivative key. `If-None-Match` gets a 304 straight from Django.
- **Original content types:** the extension allowlist only. Never sniffed.
- **Pins:** the gateway and Django tests pin these headers on both the 200 and the 304.

**Static checks:** ruff, mypy, and an nginx syntax check through the existing static parsing test, which is only written here.

## Task 6: Interactive requests, the entry `media` field, seed grants (M3, M6)

**Files:**
- `media/api.py`, `media/requests.py`;
- the catalog serializers and queries (`media` object via a `LEFT JOIN`, with no N+1);
- `seed_phase1_e2e`: grants gain PREVIEW and EXPORT where the journeys need them. Update `FixtureDrift` expectations;
- tests.

**Requirements:** as in the contract.
- Requests create an operation and job targeted at media, with idempotency (one pending per entry and kind).
- Entry lists stay within their query budget.

## Task 7: Timeline API (M5, M6)

**Files:** `media/timeline.py`, its cursor signing (salt `aegis.media.timeline.v1`), API wiring and tests.

**Requirements:** keyset on `(sort_at desc, entry_id desc)`, with permission filtering like the catalog's, and limits and bounds as in the contract.

## Task 8: Frontend — classification, thumbnails, details (M7)

**Files:**
- `frontend/src/features/files/` (`FileRow`, `EntryDetailsPanel`, types, API client);
- a new `frontend/src/features/media/` (api, types, components);
- Vitest tests.

**Requirements:**
- Extension classification shared from one module, matching M4's allowlists.
- `FileRow` shows a lazy `<img loading="lazy" decoding="async">` thumbnail when one is ready, with a fixed box that prevents layout shift; otherwise a type icon.
- The details panel shows metadata, plus Open and Download according to the permissions.
- Private-state cleanup covers the media query keys.
- No `blob:` or object URLs.

**Static checks:** typecheck and lint.

## Task 9: Frontend — Photos timeline (M7)

**Files:**
- the route `/files/:rootId/photos`;
- the Files/Photos shell navigation;
- a virtualized grid using `@tanstack/react-virtual` (rows of N tiles, month headers);
- keyset infinite query (`maxPages` 5, `gcTime 0`, namespace in the key);
- the kind filter;
- Vitest tests.

**Requirements:** at most about 80 rendered tiles, bounded prefetch and cancellation on navigation, accessible names, and focus restore on return from the viewer.

## Task 10: Frontend — Viewer (M7)

**Files:**
- `features/media/viewer/` (`Viewer`, `PhotoStage`, `VideoStage`);
- route state for the context (directory or timeline) and the current entry;
- Vitest tests.

**Requirements:**
- **Photos:**
  - pointer-event swipe and pinch, double-tap zoom, keyboard navigation and Escape;
  - shows the preview derivative, issuing a POST request when it is missing and showing the thumbnail meanwhile;
  - prefetches the next and previous previews.
- **Videos:**
  - uses `canPlayType(playbackType)` to choose the original inline URL;
  - otherwise POSTs a transcode request and polls `/api/v1/entries/{id}/media` with backoff (1 s up to 10 s, cancelled on unmount), showing "Preparing playback…";
  - uses native controls.
- **Download:** a button that links to `downloadUrl`.
- **Cleanup:** on close, restore focus and scroll; logout purges everything.

## Task 11: Deployment (M8)

**Files:**
- **Compose:**
  - `compose.yaml`: the `media-processor` service and the `media-scratch` volume, mounted into media and the processor;
  - compose test expectations (the service set, network membership, no secrets, `network_mode: none`).
- **Podman launcher:**
  - `compose.podman.yaml`: the unmask entry for the new service;
  - `container_launch.py`: canonical services allowlist;
  - `aegisctl mounts`: roots also bind into `media-processor` (compose and Quadlet render);
  - the gateway and media root binds stay.
- **Backend image:** pre-create `/srv/aegis/derivatives`, `/srv/aegis/scratch` and `/srv/aegis/quarantine`, owned by the runtime UID and GID, mode 0750 (scratch 0770).
- **Quadlet:**
  - `media-processor.container` (`Network=none`, `GIDMap`, `SecurityLabelType`, read-only, no secrets, tmpfs, `Pull=never`, restart semantics as D13) and `media-scratch.volume`;
  - the derivatives volume gains an optional bind form, used by the installer;
  - parity table updates.
- **Installer:**
  - a fifth role `media` (the `aegis-media` image) for init, upgrade and local images;
  - `--derivatives-dir` (validation and preparation as M8) on `init` and `upgrade`, recorded;
  - status shows it.
- **CI:** `publish` builds and pushes five images; workflow tests updated.
- **D12 harness:** builds five images and adds a `media_processor_isolated` check (no network interfaces besides `lo`, no secret files, read-only roots).
- **Tests:** written and updated.

## Task 12: e2e fixtures and journeys (§4)

**Files:**
- `tests/fixtures/media/`: tiny committed synthetic media, generated by a committed script `scripts/generate_media_fixtures.py` that runs in the media image (JPEG with orientation, PNG, HEIC, H.264 MP4, HEVC MOV, corrupt.jpg), each a few KB;
- `scripts/e2e_support.py` copies them into the disposable roots in place of the placeholder bytes;
- `frontend/e2e/phase2c.spec.ts`;
- the harness updates the e2e stack to include `media-processor`.

**Journeys (mobile Chromium and WebKit):**
- a thumbnail appears in Files and Photos;
- the viewer swipes and zooms;
- an MP4 plays and seeks; the HEVC MOV goes through transcode or native playback, depending on the browser;
- download has the right `Content-Disposition`;
- privacy:
  - after logout, re-login as another user shows no prior thumbnails;
  - revoking the grant makes thumbnail and original URLs return 404, and the UI clears;
  - after account switch, nothing reappears through back/forward or the cache.

Never use `page.unroute` (the guard test enforces this).

## Task 13: End-of-phase verification, parts 2 and 4 (step 12)

The controller runs, in order:
1. The static checks.
2. `make verify`.
3. `make verify-compose`, with the O11 module exception unchanged.
4. `make test-e2e`.
5. `make verify-podman-deploy`.
6. `git diff --check`.

Then fix failures, using subagents per failure class with reviews, and repeat until green. Next comes the performance work:
- a mobile grid scroll and viewer timings in e2e;
- a backfill throughput and memory probe on a synthetic library.

After that, push once and watch CI, then audit the published images (M9).

## Task 14: Upgrade, backfill, acceptance, docs (step 13)

1. Create the dedicated derivatives directory on the chosen disk.
2. `aegis-podman upgrade --release sha-<published> --derivatives-dir <dir>`.
3. Run the D6 probe for the processor, then measure the thumbnail backfill: rate, CPU, memory and disk per 10K assets, extrapolated before letting it run fully.
4. The user tests their media.
5. Update the docs for parts 2 and 4: the operator guide, the verification report, README features, HANDOFF and memory.
6. The user reboots, and the reboot check confirms everything came back.
