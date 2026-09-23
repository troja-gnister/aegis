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

## Disposable PostgreSQL probe

The documentation checkpoint `ec1f64e4066a355950908f08dc01d425ebdb1a6e` passed overall [CI run 35861161780](https://github.com/troja-gnister/aegis/actions/runs/35861161780). That run does not test the subsequent uncommitted adapter.

An independently reviewed subset of the adapter now passes the canonical focused probe:

```bash
python scripts/verify.py backend \
  --test-target backend/tests/integration/roots/test_models.py::test_database_rejects_invalid_root_modes
```

The run used Python 3.13 and explicit `AEGIS_CONTAINER_ENGINE=podman`, `PODMAN_COMPOSE_PROVIDER` and `AEGIS_PODMAN_SOCKET` values. The socket belonged to a newly created temporary rootless Unix API service; no TCP API or persistent service was enabled. Django checks and migration-drift detection passed, all three selected tests passed in 2.55 seconds, and the command exited successfully after removing its memory-only database and fixture. Subsequent engine container and volume inventories were empty.

This evidence belongs to verifier source SHA-256 `42b779d42c0c505b7ba62ab644f540f00ffe4c89bddfe9f2446464b9f8761d84`, before the adapter has a committed revision. The pulled PostgreSQL image is Linux amd64, image ID `b07129cc272f688c98f5b343138a0a52fa45b3d82f50d7a53ff441330624cd2e`, from the existing pinned PostgreSQL 18.6 Alpine reference. It is a small disposable database probe, not full backend, application deployment, browser, or scale acceptance.

Two failures preceded this passing result. Fedora's enforced short-name policy rejected the unqualified image reference without a TTY; qualification as `docker.io/library/postgres` kept its tag and digest unchanged. A later run passed the three tests but refused final file cleanup because Podman had automatically removed its CID file. The correction permits only that recorded file's disappearance after successful exact container removal and a successful absence query, while retaining complete directory/password checks. A further regression test covers partial creation with no CID file. The verifier's 48 focused tests and independent correction review passed. This behavior follows the upstream [CID-file lifecycle](https://docs.podman.io/en/latest/markdown/podman-create.1.html#cidfile-file); no registry or host security settings were changed.

## Adapter status and required next checks

The local adapter remains uncommitted. Its first correction round reported 68 resource-free tests passing and 387 deployment tests collected. Full Ruff passed, but full backend mypy found two test-only errors. Further review found unresolved resource adoption, ownership tracking, full pre-cleanup validation, absence/error distinction, partial-create recovery, preparation ordering and synthetic manifest/test setup problems. These historical results did not clear runtime preparation.

An earlier broader unit run under `backend/tests/unit/aegisctl` reported 133 passes and 61 failures; the observed failures rejected the sandbox's host-mount identity. This is retained as failed local evidence, with host-runtime verification still pending. It is not a passing mount-security gate or a reason to weaken those checks.

The subsequent correction round reports 98 combined verifier/engine/E2E-support tests, focused gateway/TLS/boundary tests, full Ruff, and mypy across 224 files passing. Independent lifecycle and compatibility reviews completed with further findings; correction round three is in progress. It covers remaining resource-scope and diagnostic-preservation gaps, the Compose verification token, explicit local Podman command routing, and intentional Caddy recreation. These resource-free results do not clear all runtime paths. Review those corrections before testing the actual application under rootless Podman, preserving Docker CI and all read-only mounts, nested-mount rejection, web-without-originals, no-egress, loopback, init/reaping, resource-limit and secret-permission assertions. Never substitute skips, rootful/privileged execution, disabled SELinux or relabeling/chown of user originals.

The application gates remain unrun on this host: `make verify`, `make verify-compose`, and `make test-e2e`. Run full resource-using harnesses sequentially. Task 14 still owns the validated test-only port override and real indexed 603-entry mobile journeys. Catalog, physical-scan and long mobile scale measurements, reference certification and fresh-checkout/upgrade acceptance remain open under Tasks 15–18.

## Cleanup disposition

The isolated browser container was stopped and removed only after checking its recorded full ID and owner label; container absence and listener disappearance were confirmed. Its uniquely owned image/build cache is retained and recorded locally. The focused PostgreSQL probe's exact container was also removed, with empty container/volume inventories confirmed. No original or large fixture was changed. Private diagnostic directories from refusal tests and the earlier CID cleanup failure are retained; names alone do not authorize their removal.

On September 23 resumption, the old temporary API process, directory and socket were absent. A new identity-recorded foreground Unix API service was created for the focused probe and remains active during prerequisite testing. Its exact process, socket and directory require identity-checked shutdown at the next stopping checkpoint. Ports 18080 and 55432 were free before the passing probe. Future runs must check again and create new owned resources with new identity records. Never infer ownership from a reusable name or delete unknown contents to complete cleanup.
