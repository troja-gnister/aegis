# Rootless Podman development environment design

**Date:** October 4, 2026

**Status:** Design approved in conversation on October 4, 2026; this written specification awaits user review. No implementation has started.

**Parent:** [HANDOFF.md](../../../HANDOFF.md) continuation; [Linux/Podman prerequisite report](../../verification/linux-podman-prerequisite.md)

**Baseline:** `main` at `6bfaa01` (Docker CI [run 37123170741](https://github.com/troja-gnister/aegis/actions/runs/37123170741): attempt 1 failed e2e only, attempt 2 passed all four jobs).

## 1. Outcome and boundary

Development of Aegis moves to a shared Fedora 44 Server development host, which runs native rootless Podman and no Docker Engine. This package gives that host a reliable, repeatable development loop: the locked toolchain, a committed helper for the private Podman API socket and the WebKit browser server, and the three canonical gates (`make verify`, `make verify-compose`, `make test-e2e`) running to completion under rootless Podman. Docker remains the primary, supported runtime; Docker CI on GitHub remains the acceptance authority and must stay green.

This is part 1 of four agreed parts:

1. **This package:** development toolchain, CI flake investigation, and Podman gates.
2. Persistent private-network deployment on this host: free ports and a firewall rule that admits only private-network sources, local-CA HTTPS, real client addresses (O11), SELinux and UID mapping for read-only originals, the user's chosen test root as the first root, and pull-and-run published images with no checkout or local build on the server.
3. The HANDOFF queue: public documentation sync for Task 15 and the web performance slice, the 65 s pool revocation question and runbook note m8, then Tasks 16–18.
4. Media viewing: authorized downloads/ranges, thumbnails and photo/video viewers (a roadmap reorder that needs its own design).

Parts 3 and 4 are ordered after part 2 is running. Each part receives its own specification, plan and review.

### Out of scope

- The O11 client-address fix (part 2).
- Any persistent deployment, published image, release tag, firewall rule or real user folder. This package uses only synthetic test fixtures.
- Public documentation status for Task 15 and the web performance slice, the revocation question, and Tasks 16–18 (part 3).
- Changes to Docker behavior. Any shared-file change must leave Docker semantics identical.

## 2. Host context

| Item | Observed October 3–4, 2026 |
| --- | --- |
| Hardware | 28 logical CPUs, 62 GiB RAM |
| Distribution | Fedora 44 Server; SELinux enforcing |
| Engine | Podman 5.8.7 (upgraded by the user from 5.8.4 on October 4), crun 1.28, netavark 1.17.2, pasta; rootless, cgroup v2 with systemd |
| Delegated cgroup controllers | `cpu`, `memory`, `pids` (no `cpuset`) |
| Storage | Podman graphroot on `/` (xfs), with more than the 40 GiB of free space the gates require (section 5.1); `/tmp` is a tmpfs |
| Pre-existing state | The host's existing rootless containers and services, with their own volumes and networks; default ports may already be taken on a shared host; choose free ports |
| Missing before this package | Python 3.13, uv, Node.js 24, a Docker Compose v2 provider, the Podman API socket |

The guarded Podman launcher (`backend/aegisctl/podman_mask_compatibility.py`) accepts exactly Podman 5.8.7, crun 1.28 and Docker Compose 2.39.4, and uses the locally built `aegis-backend` image as its probe. This host now matches the engine and runtime pins. The pins are intentionally not version-locked on the host: a later Fedora Podman update makes the guard refuse until that version is re-verified, which the README documents.

`podman compose` searches `PATH` for `docker-compose` before `podman-compose`. The host's existing services rely on podman-compose, so the Compose v2 provider for Aegis must live outside `PATH`.

## 3. Toolchain layout

All tools are installed for the current user only, pinned to CI's versions, and verified against published checksums. Host interpreters stay unchanged.

| Tool | Version | Location | Selection |
| --- | --- | --- | --- |
| uv | 0.12.8 | `~/.local/bin/uv` | On `PATH` |
| Python | 3.13.15 | uv-managed under `~/.local/share/uv/python` | Project `.venv` via `uv sync --locked --group dev` |
| Node.js | 24.20.0 | `~/.local/opt/node-v24.20.0-linux-x64/` | Prepended to `PATH` only in a shell that loads the helper's `env` output |
| Docker Compose | 2.39.4 | `~/.local/libexec/aegis/docker-compose` | Only through `PODMAN_COMPOSE_PROVIDER` |
| Playwright Chromium | from the frontend lock (1.63.0) | `~/.cache/ms-playwright` | `npm exec -- playwright install chromium` |
| Playwright WebKit | from the frontend lock (1.63.0) | Local image built from `docker/dev-webkit.Dockerfile` | Helper-managed server container |

The Compose provider file must be owned by the user and not group- or world-writable, as `podman_compose_provider()` requires. Host WebKit is not used, because the locked WebKit build needs Ubuntu library ABIs that Fedora does not provide. If host Chromium lacks a system library on Fedora Server, the implementer reports the exact packages and the user decides; nothing is installed with privileges without approval.

Image builds use the bases already pinned by digest in `deploy/images.lock` and the Dockerfiles. Expected Podman storage use for images, build cache and test volumes is about 10–20 GiB.

## 4. Helper: `scripts/podman_dev.py`

A standard-library Python module, invoked as `uv run --locked python -m scripts.podman_dev <command>`, following the existing `scripts/` conventions.

### 4.1 Commands

| Command | Behavior |
| --- | --- |
| `tools` | Installs or verifies uv, Node.js and Docker Compose into the locations in section 3. Versions, download URLs and SHA-256 values come from the tracked lock `scripts/podman_dev_tools.json`. Each lock value is checked against the publisher's checksum when recorded. Idempotent; a present file with the wrong checksum is reported and never overwritten. |
| `start` | Runs the preflight, records the inventory baseline, starts the private API service, builds the WebKit image if absent, starts the WebKit server, and writes the session record. |
| `env` | Prints shell-quoted `export` lines for `AEGIS_CONTAINER_ENGINE=podman`, `PODMAN_COMPOSE_PROVIDER`, `AEGIS_PODMAN_SOCKET`, `E2E_WEBKIT_WS_ENDPOINT` and `PATH` (Node.js 24 first). Fails unless a live session verifies. |
| `status` | Re-verifies every recorded identity and reports the session state. |
| `stop` | Identity-checked shutdown of the WebKit container and API service, followed by the inventory comparison. |

`VITEST_MAX_WORKERS` is not exported by default; the documented small-machine setting remains available.

### 4.2 Preflight

`start` refuses when any of these holds:

- effective UID 0, or Podman is not rootless;
- SELinux is not enforcing;
- `DOCKER_HOST`, `DOCKER_CONTEXT`, `CONTAINER_HOST` or `CONTAINER_CONNECTION` is set;
- `XDG_RUNTIME_DIR` is unset, not owned by the user, or not mode `0700`;
- a session record exists, whether live or stale. A stale record is reported with its identities, not silently replaced; `stop` recovers it (section 4.5).

### 4.3 Private API service

- State directory: `$XDG_RUNTIME_DIR/aegis-podman-dev/`, mode `0700`, holding `session.json`, the inventory baseline and the service log.
- Socket directory: a fresh `mkdtemp` directory (mode `0700`) inside the state directory, so `podman_socket()`'s ownership and ancestor checks pass.
- Service: `podman --remote=false system service --time=0 unix://<socket dir>/podman.sock`, started in its own session with output to the private log.
- Readiness: the socket answers a libpod info query through the Unix socket.
- Recorded identity: process ID, process start time (from `/proc/<pid>/stat`), exact command line, and the device/inode of the socket and its directory.
- Never: a TCP listener, a rootful socket, or any change to the systemd `podman.socket` unit.

### 4.4 WebKit server container

The image comes from the new tracked `docker/dev-webkit.Dockerfile`:

- base `mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27` (the digest recorded in the prerequisite report; re-verified during implementation);
- Node.js 24.20.0 copied from the `node:24.20.0-bookworm-slim` base already pinned in `deploy/images.lock`;
- `npm ci --ignore-scripts` from the tracked `frontend/package.json` and `frontend/package-lock.json`, so the Playwright server matches the client exactly.

The image is engine-neutral, so it also serves Docker developers on non-Ubuntu hosts.

Container settings:

- non-root image user, read-only root filesystem, `--cap-drop ALL`, `no-new-privileges`, default SELinux labeling, private IPC;
- 2 GiB memory, 2 CPUs, 256 PIDs, size-limited tmpfs for browser scratch space;
- no host binds, secrets or devices;
- pasta networking with exactly two forwards: container loopback port 18080 to the host's loopback e2e gateway (`-T 18080`), and one random host-loopback port to the Playwright server;
- the server runs `playwright run-server` on a random path, giving `ws://127.0.0.1:<port>/<32 lowercase hex>`, the form `validatedWebkitEndpoint()` accepts.

The recorded identity is the full container ID and the image ID. If pasta cannot provide this reachability when measured, the implementer stops and asks the user before considering host networking, which the old machine used.

Host Chromium keeps its sandbox enabled (`chromiumSandbox: true` when the endpoint is set).

### 4.5 Stop and inventory comparison

`stop` performs, in order:

1. Remove the WebKit container by its recorded full ID only, then confirm its absence.
2. Re-verify the API process ID, start time and command line, then send `SIGTERM`; wait with a bound.
3. Confirm the socket is gone, then remove the socket directory and state entries only after confirming their recorded device/inode and expected contents.
4. Compare the current Podman inventory with the baseline: container IDs, names, image IDs and creation times; volume names and creation times; network IDs and names; image IDs.

`stop` also recovers a stale session (for example after a crash, logout or reboot): when the recorded process or container is already absent, it confirms that absence by the recorded identity, then removes only the recorded entries that still match their device/inode.

Any identity mismatch stops the sequence and preserves diagnostics. The comparison reports any pre-existing object that disappeared or changed identity, and never deletes anything. The helper never prunes and never selects objects by name, label or prefix.

### 4.6 Helper tests

Resource-free tests in `tests/deployment/test_podman_dev.py`, reusing the fake-engine support in `tests/support/`, cover:

- each preflight refusal;
- state and socket directory modes and ownership;
- shell quoting of `env` output;
- `tools` checksum mismatch and refusal to overwrite;
- refusal to stop on a process ID, start-time, command-line, socket or container mismatch;
- the inventory comparison (disappeared, changed and unchanged objects).

One real `start`/`status`/`stop` smoke run on this machine is recorded in the evidence.

## 5. Gate run and triage

### 5.1 Sequence

`tools`, `start`, then sequentially `make verify`, `make verify-compose`, `make test-e2e` and `git diff --check`, followed by `stop`. Before every harness run:

- ports 18080 and 55432 must have no listeners; no unrelated service is stopped to free a port;
- free space on `/` must be at least 40 GiB, or the run stops and the user is asked.

### 5.2 Expected failures

- The stale core-services no-new-privileges assertion in `tests/deployment/test_compose.py`.
- The Podman mask and observer refusals in `tests/deployment/test_rendered_mounts.py`.
- The `--network host` case at `tests/deployment/test_database_roles.py:414`, likely needing the `pasta:-T,<db port>` treatment already used by the indexer tests.
- O11 readiness failures in `tests/deployment/test_tls_gateway.py`.
- Unknown failures from the first complete Podman e2e run.

### 5.3 Triage policy

Each failure is reproduced with the narrowest `scripts/verify.py … --test-target` selection and then classified:

| Class | Fix |
| --- | --- |
| Stale Docker-only test expectation | Pin the exact per-engine form, as done for `CapDrop` |
| Real Podman runtime difference | Fix in the engine-specific layer (`compose.podman.yaml` or engine-routed harness code) without weakening any protection |
| O11 | Measure and confirm the cause (the client address Caddy observes), then add the test ID to the O11 exception list |
| Product bug that also affects Docker | Fix with a regression test |

Never used as fixes: skips or expected-failure markers, relaxed security assertions, `:z`/`:Z`/`:U` on originals, `label=disable`, rootful or privileged execution, or name/label/prefix cleanup. Host-level or trust-model changes need user approval first. If a failure reveals hidden complexity (for example, the e2e harness needing restructuring), work stops and the scope is revisited with the user.

## 6. CI flake investigation

Docker CI attempt 1 for `6bfaa01` failed `frontend/e2e/phase2a.spec.ts:339` ("stale cursor is refused by the server and Start from the beginning restarts at page one") in mobile Chromium only; WebKit passed the same test against the same stack, and attempt 2 passed. The masked screenshot showed a recovered page one with no alert and an enabled "Load more files" button. The only code change since the last green run is `f6db82e` (four uvicorn workers and pooled web connections, no frontend change). The working hypothesis, not yet proven, is a timing race amplified by four workers on CI's four-vCPU runner.

Once `make test-e2e` passes under Podman:

1. Run only that test in mobile Chromium 50 times against one stack, with the stack and browser constrained to four CPUs; the constraint mechanism and its measured effect are recorded.
2. If the harness cannot repeat a single test, add the smallest test-only option needed and review it like any other change.
3. If it reproduces, identify the failing assertion locally, fix the root cause, and add a regression check.
4. If it does not reproduce, record repeat counts and conditions as evidence and continue watching CI.

## 7. Evidence and documentation

- **Verification report:** a new dated section in `docs/verification/linux-podman-prerequisite.md` recording the host environment table (versions, checksums, WebKit image digest), each gate's counts and durations, every failure with cause and fixing commit, the O11 exception list by test ID, the inventory comparison result, and the flake investigation result.
- **README and `docs/development.md`:** the Podman paragraphs state the measured gate status instead of "has not run". The README's Podman section gains a short "Developing on Podman" quickstart (`tools`, `start`, `eval "$(… env)"`, the `make` targets, `stop`) and the procedure when a Podman update makes the guard refuse. Docker stays the primary runtime, and full Podman compatibility is not claimed while O11 is open.
- **`HANDOFF.md`:** updated so the next session starts at part 2. Task 15 and web performance status lines stay for part 3.

## 8. Workflow and safety

- Work happens on `main` in scoped commits made by the root session. Every push to `main` is asked first and its CI watched with `gh`.
- Implementation uses subagent-driven development: one implementer at a time, written briefs in the ignored `.superpowers/sdd/<plan>/`, independent specification and quality review per task, at most five fix rounds, progress recorded in `progress.md`. Implementers do not commit or spawn agents.
- Harnesses run sequentially. An interrupted run uses its own exact-identity recovery; retained resources are recorded and left in place.
- Pre-existing containers, volumes, networks and images on the host are never test or cleanup targets. No originals, user libraries or production databases are touched.
- No secrets, generated mounts, raw logs, private screenshots or browser storage are committed.

## 9. Acceptance

1. In a fresh shell, `tools`, `start` and `eval "$(… env)"` succeed, then `make verify`, `make verify-compose`, `make test-e2e` and `git diff --check` each run to completion, sequentially.
2. `make verify` passes completely (backend and frontend).
3. `make verify-compose` passes except tests whose failures are measured and attributed to O11, listed by test ID in the report. No test is skipped.
4. `make test-e2e` passes 22/22 under Podman, with Chromium on the host and WebKit through the helper container.
5. After `stop`, no Aegis-created container, socket or state directory remains, and the inventory comparison shows no pre-existing object removed or changed.
6. Docker CI passes all four jobs on the pushed result.
7. The `phase2a.spec.ts:339` flake is fixed at its root cause, or the non-reproduction is documented with repeat counts and conditions.
8. The helper's tests pass, and Ruff and mypy remain clean.

## 10. Risks and open measurements

- The complete e2e harness has never run under Podman; unknown failures may enlarge the work and trigger re-scoping.
- Pasta reachability for the WebKit container is unmeasured (section 4.4).
- Host Chromium may need Fedora Server system libraries (section 3).
- The previous Podman evidence came from Fedora Silverblue on Btrfs; this host uses Fedora Server on xfs.
- Test resources share Podman storage with the host's existing services; the inventory comparison and exact-identity cleanup are the safeguards.
- Each future Podman update requires re-verification before the guarded launcher accepts it.
