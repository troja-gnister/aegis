# Linux/Podman prerequisite checkpoint

Observed September 22–23, 2026, from repository checkpoint `ab57e3037b9daed79aeb691eb486a4d2397ae4aa`, whose last application change is `a82db75d9949170e1d22acfed0f08050f067edf2`. This report records preparation evidence, not application Podman acceptance. Phase 2A.1 remains **18 tasks: 12 complete, 6 remaining**; Task 13 has not started.

## Actual environment

| Item | Observed value |
| --- | --- |
| Architecture / CPU | x86-64, Intel Core i7-1355U, 10 cores / 12 logical CPUs |
| Distribution / kernel | Fedora Silverblue 44.20260921.0; Linux 7.2.5-200.fc44.x86_64 |
| Memory / storage | About 30 GiB RAM; Btrfs, initially about 44 GiB free and about 42 GiB after toolchain preparation; `/tmp` is a 16 GiB tmpfs |
| Engine | Podman 5.8.7, rootless, cgroup v2 with systemd; crun 1.28 |
| Networking | netavark 1.17.2, aardvark-dns 1.17.1, pasta |
| UID mapping | Container UID 0 maps to host UID 1000; container UIDs 1–65536 map from host UID 524288 |
| SELinux | Enforcing, verified outside the command sandbox; its sandbox-only `getenforce` result was misleading |
| Compose provider | Standalone Docker Compose client 2.39.4, explicitly selected by `PODMAN_COMPOSE_PROVIDER`; no Docker engine installed |
| Locked tools | uv 0.12.8, Python 3.13.15, Node.js 24.20.0; tracked uv/npm locks unchanged |
| Browsers | Playwright 1.63.0; Chromium 153.0.8010.12; WebKit 26.6 |

The development host is not the calibrated four-core N100/16 GiB reference. Capacity and inode suitability must be checked again before large fixtures; no million-entry fixture was generated. Neither `/tmp` nor the checkout is an approved physical-scale source location.

The provider executable's SHA-256 is `7af95166a730b87e172d4fc9aefea8725d3c6c7327d59149267b452114ddb7d4`. `podman compose version` succeeded. An empty-project `ps --all --format json` succeeded through an explicitly owned temporary Unix API socket. The default API socket was inactive. These checks do not establish `up --wait`, application network isolation, mount attestation, init/reaping or enforced resource limits.

Podman Compose invokes an external provider; a shell alias cannot adapt Python subprocesses. Volume ownership/relabel options must not be applied automatically to originals. See the upstream [Compose documentation](https://docs.podman.io/en/latest/markdown/podman-compose.1.html) and [volume options](https://docs.podman.io/en/latest/markdown/podman-run.1.html).

## Commands and scoped results

These results use the unchanged accepted frontend and locally installed locked tools. They are not results for the uncommitted adapter.

| Command / check | Result |
| --- | --- |
| `uv sync --locked --group dev --python 3.13` | Locked Python environment installed |
| `uv lock --check --offline` | Passed |
| `npm --prefix frontend ci` | Locked installation passed; no dependency upgrades |
| `npm --prefix frontend test` | 130/131 passed; the million-step navigation test took 5,558 ms against its existing 5,000 ms timeout |
| Focused `navigation.test.ts` | 4/4 passed in 4.43 seconds |
| `npm --prefix frontend test -- --maxWorkers=1` | 131/131 passed across 12 files in 20.54 seconds; assertions/timeouts unchanged |
| Frontend lint, typecheck and production build | Passed |
| Host Chromium synthetic data-page button check | Passed with `chromiumSandbox: true`; CDP confirmed no `--no-sandbox` launch argument |
| Isolated WebKit synthetic data-page button check | Passed; no application or credentials involved |

Host WebKit could not launch: the locked fallback build requires ICU 74, JPEG ABI 8 and GStreamer libav, while this Fedora host has different library ABIs. No host packages or security policy were changed. An isolated amd64 browser image supplied the compatible libraries, using official base `mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27`, the checksum-verified Node 24 binary and tracked npm lock. Installation disabled dependency lifecycle scripts. The resulting local image ID is `a31dc1bd03e64867c432b2c0bfbe48f3f2318bac3dca4a5ad24dfb9c472a3dd6`.

Before starting that browser container, inspection confirmed rootless execution, non-root UID, read-only root, no capabilities, no-new-privileges, enforcing SELinux, private IPC, bounded tmpfs, 2 GiB memory, 2 CPUs and 256 PIDs, and no host bind mounts, devices, secrets or published ports. Readiness checked the exact endpoint and sole IPv4 loopback listener. Its host-network setting permits host/LAN/egress access and is an explicit development-browser exception for trusted synthetic content; it provides no application outbound-isolation evidence. Chromium stayed on the host with its sandbox enabled; only WebKit used the remote endpoint. No unsafe Playwright server mode was enabled.

The first remote-client attempt was blocked by the command sandbox's loopback restriction; the approved retry passed. Temporary browser configuration and synthetic scripts remain local preparation tools, not a shipped cross-platform harness or real-stack acceptance gate.

## Adapter status and required next checks

The local, uncommitted adapter is under independent review. Its first correction round reported 68 resource-free tests passing and 387 deployment tests collected. Full Ruff passed, but full backend mypy found two test-only errors. Further review found unresolved resource adoption, ownership tracking, full pre-cleanup validation, absence/error distinction, partial-create recovery, preparation ordering and synthetic manifest/test setup problems. These results do not clear runtime preparation; correction work is in progress.

An earlier broader unit run under `backend/tests/unit/aegisctl` reported 133 passes and 61 failures; the observed failures rejected the sandbox's host-mount identity. This is retained as failed local evidence, with host-runtime verification still pending. It is not a passing mount-security gate or a reason to weaken those checks.

Complete and review those boundaries before the smallest canonical disposable PostgreSQL probe. Then test the actual application under rootless Podman, preserving Docker CI and all read-only mounts, nested-mount rejection, web-without-originals, no-egress, loopback, init/reaping, resource-limit and secret-permission assertions. Never substitute skips, rootful/privileged execution, disabled SELinux or relabeling/chown of user originals.

The application gates remain unrun on this host: `make verify`, `make verify-compose`, and `make test-e2e`. Run full resource-using harnesses sequentially. Task 14 still owns the validated test-only port override and real indexed 603-entry mobile journeys. Catalog, physical-scan and long mobile scale measurements, reference certification and fresh-checkout/upgrade acceptance remain open under Tasks 15–18.

## Cleanup disposition

The isolated browser container was stopped and removed only after checking its recorded full ID and owner label; container absence and listener disappearance were confirmed. Its uniquely owned image/build cache is retained and recorded locally. No application container, database, mounted original or large fixture was started or changed.

On September 23 resumption, the old temporary API process, directory and socket were absent. Rootless Podman listed no containers, and ports 18080, 55432 and the former browser port had no listeners. Future runs must check again and create new owned resources with new identity records. Never infer ownership from a reusable name or delete unknown contents to complete cleanup.
