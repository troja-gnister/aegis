# Linux/Podman prerequisite checkpoint

Observed September 22–24, 2026, beginning from repository checkpoint `ab57e3037b9daed79aeb691eb486a4d2397ae4aa`, whose last accepted application change is `a82db75d9949170e1d22acfed0f08050f067edf2`. Reviewed engine/verification tooling is committed at `163c02563d95de8e4e3930ae68377e946feef3b8`; guarded duplicate-mask compatibility and focused mount regressions are committed at `71da40ed34bbe05990093afbf12925df40462ccf`. PostgreSQL/Caddy tmpfs adaptations and complete application gates remain open. This report records scoped successes and failed application-gate evidence, not full Podman acceptance. Phase 2A.1 remains **18 tasks: 12 complete, 6 remaining**; Task 13 has not started.

## Actual environment

| Item | Observed value |
| --- | --- |
| Architecture / CPU | x86-64, Intel Core i7-1355U, 10 cores / 12 logical CPUs |
| Initial distribution / kernel | Fedora Silverblue 44.20260921.0; Linux 7.2.5-200.fc44.x86_64 |
| September 24 booted distribution / kernel | Fedora Silverblue 44.20260922.0; Linux 7.2.6-200.fc44.x86_64 |
| Memory / storage | About 30 GiB RAM; Btrfs, initially about 44 GiB free and about 42 GiB after toolchain preparation; `/tmp` is a 16 GiB tmpfs |
| Engine | Podman 5.8.7, rootless, cgroup v2 with systemd; crun 1.28 |
| Networking | netavark 1.17.2, aardvark-dns 1.17.1, pasta |
| UID mapping | Container UID 0 maps to host UID 1000; container UIDs 1–65536 map from host UID 524288 |
| SELinux | Enforcing, verified outside the command sandbox; its sandbox-only `getenforce` result was misleading |
| Compose provider | Standalone Docker Compose client 2.39.4, explicitly selected by `PODMAN_COMPOSE_PROVIDER`; no Docker engine installed |
| Locked tools | uv 0.12.8, Python 3.13.15, Node.js 24.20.0; tracked uv/npm locks unchanged |
| Browsers | Playwright 1.63.0; Chromium 153.0.8010.12; WebKit 26.6 |

The development host is not the calibrated four-core N100/16 GiB reference. Capacity and inode suitability must be checked again before large fixtures; no million-entry fixture was generated. Neither `/tmp` nor the checkout is an approved physical-scale source location.

The September 24 native and owned-Unix-API identity checks agreed on the booted kernel and rootless engine/runtime/storage identity. `rpm-ostree status --json` reported booted deployment `b4eaddb34714f38b08068de77275941e475b83226400907d751cbcaa09b076d6`; a newer 44.20260923.0/kernel 7.2.7 deployment was staged but not booted. No host upgrade, reboot or security-policy change was performed for this work. Earlier measurements retain their original environment identity.

The provider executable's SHA-256 is `7af95166a730b87e172d4fc9aefea8725d3c6c7327d59149267b452114ddb7d4`. `podman compose version` succeeded. An empty-project `ps --all --format json` succeeded through an explicitly owned temporary Unix API socket. The default API socket was inactive. These checks do not establish `up --wait`, application network isolation, mount attestation, init/reaping or enforced resource limits.

Podman Compose invokes an external provider; a shell alias cannot adapt Python subprocesses. Volume ownership/relabel options must not be applied automatically to originals. See the upstream [Compose documentation](https://docs.podman.io/en/latest/markdown/podman-compose.1.html) and [volume options](https://docs.podman.io/en/latest/markdown/podman-run.1.html).

## Commands and scoped results

These initial results use the unchanged accepted frontend and locally installed locked tools. They precede the adapter implementation.

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

The committed tooling checkpoint covers explicit engine routing, resource ownership/recovery, test support and validated external WebKit configuration; application runtime compatibility remains open. Its first correction round reported 68 resource-free tests passing and 387 deployment tests collected. Full Ruff passed, but full backend mypy found two test-only errors. Further review found unresolved resource adoption, ownership tracking, full pre-cleanup validation, absence/error distinction, partial-create recovery, preparation ordering and synthetic manifest/test setup problems. These historical results did not clear runtime preparation.

An earlier broader unit run under `backend/tests/unit/aegisctl` reported 133 passes and 61 failures; the observed failures rejected the sandbox's host-mount identity. This is retained as failed local evidence, with host-runtime verification still pending. It is not a passing mount-security gate or a reason to weaken those checks.

Subsequent corrections passed independent lifecycle and compatibility reviews, including resource-scope validation, diagnostic preservation, the Compose verification token, explicit local Podman command routing, native network inspection, intentional Caddy recreation and a validated WebKit-only endpoint. The reviewed local source snapshot contained 33 files, with manifest SHA-256 `f5676185f4130714238cdd4d1b8ab647983aeb614abd261aadcdeaf0e13513c5`. Full Ruff and mypy across 225 files passed before actual runtime gates began. Review clearance did not establish runtime compatibility.

### First full gates and isolated diagnosis

The following sequential commands used that frozen local snapshot, locked Python/uv/Node tools, explicit rootless Podman/provider/private-socket selection, and the canonical disposable database runner. `make verify` also used `VITEST_MAX_WORKERS=1`, matching the documented frontend timing profile.

| Command | Observed result |
| --- | --- |
| `make verify` | Ruff, mypy, Django checks and migration-drift detection passed; backend **1,440 passed, 6 failed** in 297.00 seconds; exit 2. Frontend stage did not run. |
| `make verify-compose` | Hermetic Compose configuration and pinned image builds passed; deployment **359 passed, 71 failed, 10 setup errors** in 320.83 seconds; exit 2. |
| `make test-e2e` | Not run; real-stack browser evidence remains open. |
| `git diff --check` | Passed for the reviewed source. |

The six backend failures were obsolete observer subprocess doubles. Deployment failures included additional Docker-specific fake assumptions, incomplete operation-specific resource admission, unsupported tmpfs ownership options, and mount attestation refusing an ambiguous mountpoint. A role-init recovery error also hid the original create failure. These are failed gates, not skips. The deployment tool output exceeded its capture limit; the summary and key errors were retained, but no complete raw command log is claimed for that first attempt.

The observer/fake-engine/admission/diagnostic corrections subsequently passed independent specification and quality review. The affected observer tests passed all 12 explicit Docker/Podman cases in 0.24 seconds, and nine fingerprint regressions passed in 0.26 seconds. The wider resource-free correction selection passed 117 tests; Ruff, mypy and diff checks passed. Their 37-file source manifest is `85cc7251940e4236e84b8ca8ad9142aedac59aac04febd8827de1b9a5e404f92`. Actual application mount compatibility remains separate from those test-harness corrections.

That exact source was then committed as `163c02563d95de8e4e3930ae68377e946feef3b8` after a successful canonical `make verify` run on the September 24 environment. **All 1,452 backend tests passed in 747.16 seconds; all 147 frontend tests across 13 files passed in 28.06 seconds.** Locked dependency checks/install, Ruff, mypy across 225 sources, Django checks, migration-drift detection, frontend lint/typecheck/build and `git diff --check` passed. The command exited 0, removed its disposable database, and a subsequent container inventory was empty. Complete local command output was retained for this run. `make verify-compose` and `make test-e2e` have not passed for this tooling revision; the earlier deployment failures still require runtime fixes.

The pushed documentation checkpoint `db6e10765e000cee7335244444b21434079bdac1`, containing that tooling revision, failed [CI run 35989701646](https://github.com/troja-gnister/aegis/actions/runs/35989701646): backend and frontend passed; deployment and browser journeys failed. Those Docker failures require diagnosis and correction in addition to the remaining local Podman blockers.

A bounded, no-bind/no-secret/no-network probe reproduced Podman 5.8.7's rejection of `uid=0` in tmpfs options. The configured PostgreSQL mounts require different owners within one container; deleting those requirements is not an acceptable compatibility fix. A reviewed design can prepare only the verified empty container-local destination tmpfs directories using PostgreSQL's existing privileged startup exception, while preserving source-secret protection, exact final ownership/modes and the UID-70 privilege drop. That design is not yet implemented or runtime-accepted; no host-input ownership change is authorized by it.

A subsequent two-container diagnostic on September 24 measured the pinned PostgreSQL image's four fresh private tmpfs directories as UID/GID 0 with their configured modes, with no directory entries reported. It confirmed the shell tools needed for a bounded bootstrap and the image's `/var/run -> ../run` alias. Reading the pinned official entrypoint confirmed that it changes the socket directory to mode `03775`; compatibility work must preserve that existing behavior. The diagnostic performed no ownership changes, secret staging or database startup. Its second container confirmed that `U` gives a newly created private tmpfs the same UID/GID 10001 as its container user, while retaining mode `0700` and `noexec,nosuid,nodev`. This native-only result does not establish Caddy or Compose compatibility and does not authorize applying `U` to host binds. Both exact-owned containers and fixtures were removed; the final container inventory was empty.

A separate pinned-Caddy-image diagnostic subsequently passed through native Podman and Compose 2.39.4 using `U,notmpcopyup` only on newly created private tmpfs. Both `/config` and `/tmp` were empty, owned by 10001:10001, limited to 16 MiB, and protected by `noexec,nosuid,nodev`; their modes were `0700` and `1777`. Four earlier attempts refused on option-representation or initial-content checks; one exposed the image directory copied into `/config` by default. All exact-owned resources from those attempts and the successful run were removed. Compose warned that requested OOM score adjustment 0 was raised to the inherited 100; no OOM-score equivalence is claimed. This proves the bounded mount configuration, not Caddy server startup, writes, recreation or application compatibility. No host bind received ownership or label preparation.

The earlier no-bind mount probe found two read-only masks at `/sys/devices/virtual/powercap`, even with `--read-only-tmpfs=false`. Tagged Podman 5.8.7 source includes this path in its [default mask list](https://github.com/podman-container-tools/podman/blob/v5.8.7/vendor/go.podman.io/common/pkg/config/default.go#L40) and [appends it again during final specification generation](https://github.com/podman-container-tools/podman/blob/v5.8.7/libpod/container_internal_linux.go#L804). Disabling automatic tmpfs does not resolve this failure.

On September 24, a separate native/Compose probe confirmed a narrowly scoped candidate: `unmask=/sys/devices/virtual/powercap` suppresses the first entry while this exact Podman version's later step retains one mask. Before executing each payload, `podman init` materialized the final OCI specification for inspection. Both transports changed from 25 mask entries with two powercap entries to 24 with one; the effective mask set, read-only path list, read-only root, no-new-privileges, capabilities and SELinux protections were preserved. After startup, powercap remained empty or inaccessible and read-only, the unchanged mount parser passed, and the OCI checks still matched. All four exact-owned containers/fixtures were removed. This is isolated diagnostic evidence, not a production workaround: application integration must enforce version/behavior checks. Class-directory aliases were not separately exercised, and no application original mount or network-isolation claim follows from these probes. An earlier Compose attempt failed because the temporary API had stopped; that failed attempt's fixtures were also removed before retrying through a newly owned endpoint.

The guarded application adapter subsequently passed independent review and its shared four-profile check on frozen source manifest `03b541bf4bc318026bd6f4b47232bfa5e1d68ff989b66bd315628125c94afcc2`. The check completed in 16.96 seconds using native Podman and Compose, with no originals, secrets, host binds or network. Podman's explicit null capability-list representation is accepted only alongside empty initialized/final OCI capability sets and five zero kernel capability bitmaps. Both transports retained the effective masks and read-only paths while removing the duplicate; all four exact-owned containers were removed and the final container inventory was empty. The adapter requires fresh behavior checks before each guarded launch and retains the strict mount parser. Focused verification passed 303 engine/adapter tests and 76 host-topology unit tests. Actual application mounts, nested-mount rejection and complete service compatibility still require their runtime gates; this isolated success does not advance a product task.

The next canonical disposable deployment selection covered the rendered observer and nested-mount tests after two reviewed test-routing corrections. It failed with **1 failure, 4 passes and 21 setup errors** in 2.41 seconds, before any source-mounted container started. The engine identity check refused a timezone-dependent `version.BuiltTime` difference: Django's UTC setting and the API service's local timezone produced different display strings for the same numeric build timestamp. Read-only comparison confirmed every other selected identity field matched. The disposable database was removed and the final container inventory was empty. Complete command output was retained.

The reviewed correction validates the numeric build timestamp and both bounded display strings, then excludes only the redundant display field from native/API comparison. Native-client/native-info equality, all other identity fields, raw evidence and final identity revalidation remain exact. The corrected engine/adapter suite passed **346 tests**. The following canonical selection then completed with **25 passes and one failed assertion** in 424.42 seconds:

```bash
uv run --locked python scripts/verify.py deployment \
  --test-target tests/deployment/test_rendered_mounts.py::test_real_observer_leaves_no_unique_project_resources \
  --test-target tests/deployment/test_nested_mounts.py
```

The passing cases included the rendered observer, all four native leaf-role checks, all 16 nested role/mode/depth cases and four fake routing cases. The remaining real observer correctly rejected its injected descendant mount; the test incorrectly matched the specific reason against the outer retained-diagnostics message instead of its direct cause. A test-only correction now requires both errors and preserves exact-once injection. All **10** targeted fake cases passed, including refusal of missing, wrong-type and unrelated causes for both engines. Independent specification and code-quality review passed. The affected actual case then passed in **37.35 seconds**:

```bash
uv run --locked python scripts/verify.py deployment \
  --test-target tests/deployment/test_nested_mounts.py::test_real_observer_rejects_descendant_mount_visible_only_in_container
```

These runs used the explicit Podman/provider/private-socket environment above, enforcing SELinux and owned synthetic sources. Both disposable databases were removed; final container inventories were empty, and source-sentinel checks reported no changes. Failure diagnostics and complete command logs were retained. The final reviewed 41-file source manifest is `c081688df179a535eea9614941d91c41f36b65be2d7c35f84403dc227d9d8feb`, committed as `71da40ed34bbe05990093afbf12925df40462ccf`. This closes the focused duplicate-mask and nested-mount regressions; it does not replace the full deployment, network, init/reaping, browser or source-manifest gates. The Podman overlay is selected by guarded launch interfaces, not a standalone operator recipe.

The pushed documentation checkpoint `2857a3168bccee34e9116d1ee2493caa5658be0b`, containing mask source `71da40e`, failed [CI run 36027572227](https://github.com/troja-gnister/aegis/actions/runs/36027572227). Backend and frontend passed; deployment and browser journeys failed. Hermetic Compose rendering and pinned image builds passed before deployment step 7 failed in the disposable database-role/runtime tests. The browser job installed frozen dependencies and both locked browsers before step 8 failed in the gateway journey. Public metadata locates these failures but does not establish their causes. These failures remain open independently of the focused local Podman success. PostgreSQL's fixed private-tmpfs bootstrap is the next prerequisite slice, followed by Caddy's private-tmpfs configuration. No product task advances.

Continue preserving Docker CI and all read-only mounts, nested-mount rejection, web-without-originals, no-egress, loopback, init/reaping, resource-limit and secret-permission assertions. Never substitute skips, rootful/privileged execution, disabled SELinux or relabeling/chown of user originals. Run full resource-using harnesses sequentially. Task 14 still owns the validated test-only port override and real indexed 603-entry mobile journeys. Catalog, physical-scan and long mobile scale measurements, reference certification and fresh-checkout/upgrade acceptance remain open under Tasks 15–18.

## Canonical merging and private-tmpfs checks

Canonical merge correction `520cf1f5bb9d5126953d33b0ce37751080ef7db8` removes the overlay's duplicate NNP declaration. Compose 2.39.4 rejected the repeated base/overlay option before starting PostgreSQL. The corrected overlay contributes only the mask option; each effective service retains NNP from the base. Actual configuration rendering passed with no profile, `tls`, `tls-local` and both profiles, with every service otherwise unchanged. The affected unit file passed **290 tests**; independent review passed, including seven focused checks. This correction does not alter Docker configuration or the guarded runtime policy.

The separately reviewed, uncommitted PostgreSQL candidate still needs runtime diagnosis. It validates the four fixed private tmpfs mounts before preparing their directory ownership, with effective engine provenance checked on the recorded container before startup. Resource-free verification passed 153 helper/fixture cases and 93 launch-order cases. The canonical command below has not passed:

```bash
uv run --locked python scripts/verify.py deployment \
  --test-target tests/deployment/test_container_boundaries.py::test_postgres_stages_fixed_source_secrets_into_uid_70_private_tmpfs \
  --test-target tests/deployment/test_container_boundaries.py::test_live_postgres_stages_secrets_and_drops_to_uid_70
```

| Local candidate | Result |
| --- | --- |
| Reviewed 54-file manifest `a77c0a5714b491d83c1cdeebdea10eb1bf1146a8f98913fe3e6ed955c39f2c88` | **2 failed in 1.12s**: duplicate security options prevented rendering; the new bootstrap did not run. |
| Same candidate plus merge correction, manifest `1ee6407b63fb308142e4b0e810b420390559af8cb75271eb44c8b087a7c18d7f` | **2 failed in 15.77s**: an old static assertion omitted the approved mask option; the recorded bootstrap container passed mount-provenance checks but exited 1. The refusal's cause is not yet established. |

Both commands retained complete logs, removed their disposable databases and recorded containers, and ended with empty container inventories. The bootstrap failure does not establish secret staging, live UID/capability drop, reconciliation or rotation compatibility. Source snapshots and ownership assertions remain required; no runtime refusal is treated as a skip or successful acceptance.

A separate isolated Caddy diagnostic did pass on the unchanged helper bundle from `71da40e`, after independent review and correction of its shell directory-enumeration checks. Diagnostic script SHA-256 `1dc8079aaddfa8b56409d4b3f66ce379fe52dc7fc07f6431901b5b419f70e966` used pinned image `ebbf5210d94567392591a0a07d61279a8be8c9041091c1713427ffa23e0f842b`, a fresh shared mask check, and two native plus two Compose generations. All four measured UID/GID 10001, zero kernel capabilities, NNP, absent Caddy file capabilities, initially empty private `/config` and `/tmp` mounts, exact 16 MiB limits and 0700/1777 modes. Tiny scratch writes/readback and empty recreation passed. The unchanged full mount parser accepted each complete snapshot with one retained powercap mask. Exact cleanup completed, the command exited 0, and the final container inventory was empty.

Those Caddy probes used no host binds, secrets, published ports, network or server. The inherited OOM score remained 100; general resource enforcement is not certified. Canonical Caddy `!override` rendering, server startup, autosave, TLS and durable certificate storage remain pending. No Phase 2A.1 task advances from these prerequisite checks.

## Cleanup disposition

The isolated browser container was stopped and removed only after checking its recorded full ID and owner label; container absence and listener disappearance were confirmed. Its uniquely owned image/build cache is retained and recorded locally. The focused PostgreSQL probe and full-gate outer database containers were removed. The isolated mount probes also completed exact cleanup, with empty container inventories confirmed. No original or large fixture was changed. Private diagnostic directories from refusal tests and the earlier CID cleanup failure are retained; names alone do not authorize their removal.

The first full deployment attempt retained **18 volumes and 23 test networks**, plus the unrelated default Podman network. Seven invalid-host TLS probes encountered declared resources omitted from their allowed transitions; two literal-dollar path probes omitted a declared database volume. Cleanup therefore refused. Later observation recorded their identities but is not proof of creation-time ownership or unchanged identity throughout the run. These resources remain untouched; corrected admission rules do not retroactively authorize cleanup.

On September 23 resumption, the old temporary API process, directory and socket were absent. A new identity-recorded foreground Unix API served the probes but had stopped by September 24. Its exact stale socket/directory were removed only after confirming the recorded identities, complete contents, absent process and absent listener. A newly owned foreground Unix API is active during continuing prerequisite testing; its exact process, socket and directory require identity-checked shutdown at the next stopping checkpoint. Ports 18080 and 55432 were free before the initial passing probe. Future runs must check again and create new owned resources with new identity records. Never infer ownership from a reusable name or delete unknown contents to complete cleanup.
