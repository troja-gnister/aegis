# Rootless Podman private-network deployment: implementation plan

**Spec:** [2026-10-10-podman-lan-deployment-design.md](../specs/2026-10-10-podman-lan-deployment-design.md). Read it first. Decisions are cited by number (D1–D12).

**Workflow:**
- Subagent-driven development: one implementer at a time.
- Test first: write each new test and see it fail for the expected reason before implementing.
- Implementers never commit, push or start long-lived containers unless the task says so. The root session commits after review.
- Host safety rules from HANDOFF apply throughout:
  - never touch pre-existing Podman objects, units or firewall rules;
  - delete only exact recorded identities;
  - no host-specific names, addresses, ports, zones or paths in tracked files;
  - no `:z`/`:Z`/`:U`, `chown` or `label=disable` on originals.

**Conventions:**
- **Installer:** `deploy/podman/aegis-podman` is a single executable Python 3 file using only the standard library. It must run on the host's system Python, 3.11 or newer.
- **Templates:** Quadlet templates live under `deploy/podman/quadlet/`. The only substitution syntax is `@@NAME@@`, and rendering fails on any unknown or leftover token.
- **Unit and object names:** every unit and Podman object name is `<prefix><name>`. The prefix is `aegis-` in production; the D12 harness uses a unique test prefix.
- **Tests:** they go in `tests/deployment/`. Tests that need a container engine use the existing `CONTAINER_COMMAND`/`run_container` helpers, so they run on both Docker and Podman.

---

## Task 1: Quadlet templates and renderer core

**Goal:** Podman units that mirror `compose.yaml`, rendered by a pure function (D1, D2, D5, D6).

**Files:**
- Create `deploy/podman/quadlet/`:
  - networks: `backend.network`, `edge.network`, `tls-hop.network`;
  - volumes: `postgres-data.volume`, `indexer-coordination.volume`, `staging.volume`, `derivatives.volume`, `quarantine.volume`, `caddy-data.volume`;
  - containers: `postgres.container`, `migrate.container`, `web.container`, `operations.container`, `indexer.container`, `media.container`, `gateway.container`, `caddy.container`;
  - socket: `caddy.socket`.
- Create `deploy/podman/aegis-podman` with the rendering core only:
  - `render_units(config) -> dict[str, str]`, from file name to text;
  - token substitution and validation.
- Tests: `tests/deployment/test_podman_deploy_render.py`.

**Unit requirements:**
- **Networks:**
  - `backend` and `tls-hop` use `Internal=true`; `edge` does not.
  - Each sets `NetworkName=@@PREFIX@@<name>`.
- **DNS names:** each container joins its compose networks with per-network aliases equal to the compose service name, so `postgres`, `web` and `gateway` resolve as they do under compose. The gateway also gets `tls-gateway` on `tls-hop` only (`Network=<unit>.network:alias=gateway,alias=tls-gateway`).
- **Security settings, as compose has them for each service:**
  - `User=`, `ReadOnly=true`, `Tmpfs=`;
  - `DropCapability=ALL` and the PostgreSQL `AddCapability=` list;
  - `NoNewPrivileges=true`;
  - `PodmanArgs=--init` for the indexer;
  - `Unmask=/sys/devices/virtual/powercap` on every container;
  - Caddy's tmpfs mounts use the `U,notmpcopyup` form from `compose.podman.yaml`.
- **Secrets:** read-only binds `Volume=@@SECRETS_DIR@@/<secret-name>:<compose target>:ro` (fix round 1; spec D9). Compose short targets resolve to `/run/secrets/<target>`.
- **Fixed backend addresses (fix round 1; spec D1):** `backend.network` uses `Subnet=@@BACKEND_SUBNET@@` with `IPRange=` set to the upper /25. web takes `.10` and the gateway `.11`.
- **Health checks:** `HealthCmd`, `HealthInterval`, `HealthTimeout`, `HealthRetries` and `HealthStartPeriod` copy compose. Long-running services set `Notify=healthy`.
- **Ordering:**
  - `migrate` is `Type=oneshot` with `RemainAfterExit=yes`, after PostgreSQL.
  - web, operations, indexer and media use `Requires=` and `After=` on migrate.
  - The gateway is ordered after web; Caddy after the gateway.
  - Each long-running service has `[Install] WantedBy=default.target`, and `Restart=on-failure` with a bounded `StartLimitBurst`. Containers themselves get no Podman restart policy.
- **ID mapping (D5):** `GIDMap=+g<container gid>:@@@HOST_GID@@:1` on the gateway (gid 101), and on operations, indexer, media and web (gid 10001).
- **SELinux type (D6):** `SecurityLabelType=@@READER_TYPE@@` on the gateway, operations, indexer and media only.
- **Environment (production):**
  - `AEGIS_ENV=production` and `DJANGO_SETTINGS_MODULE=aegis.settings.production`;
  - `AEGIS_RELEASE_ID=@@RELEASE@@`;
  - `AEGIS_PUBLIC_URL=@@PUBLIC_URL@@` and `AEGIS_ALLOWED_HOSTS=@@ALLOWED_HOSTS@@`;
  - each role's database user, password file and key file variables, as compose sets them;
  - scan tunables at compose defaults.
- **Images:** `Image=@@IMAGE_BACKEND@@`, and likewise for the gateway, PostgreSQL and Caddy. These are digest references at install time.
- **Caddy:**
  - `Exec=caddy run --config /etc/caddy/Caddyfile.lan --adapter caddyfile`;
  - environment `AEGIS_LAN_SITES=@@LAN_SITES@@`;
  - `Volume=@@PREFIX@@caddy-data.volume:/data`;
  - no `PublishPort`.
- **`caddy.socket`:** a single `ListenStream=@@HTTPS_PORT@@` (both address families), with `[Install] WantedBy=sockets.target`. The service is `<prefix>caddy.service`, so systemd hands it the socket.
- **No port publishing:** no unit publishes a port.

**Renderer:**
- Values are validated before substitution:
  - prefix `^[a-z][a-z0-9-]{0,30}-$`;
  - port 1024–65535;
  - release `^sha-[0-9a-f]{40}$`, or a test literal allowed only by an explicit flag;
  - image references must be `name@sha256:<64 hex>`;
  - host gid a positive integer;
  - names RFC 1123 hostnames or IP literals, with no wildcards or commas inside a single name.
- Any value containing a newline, `@@` or a NUL byte is refused.

**Tests:**
- Rendering with sample values yields exactly the expected file set.
- No leftover tokens; every unknown token is refused.
- Each invalid value class is refused.
- No `PublishPort` anywhere.
- The tls-hop alias is present only on the gateway's tls-hop network line.
- `GIDMap` and `SecurityLabelType` appear exactly on the D5 and D6 service sets.

---

## Task 2: Parity with compose and the Quadlet dry-run

**Goal:** D10. Drift between `compose.yaml` and the templates fails a test.

**Files:**
- `tests/deployment/test_podman_deploy_parity.py`;
- move `rendered_compose` from `test_compose.py` into `tests/support/` if it needs sharing, keeping `test_compose.py` behavior identical.

**Behavior:**
- Render compose with `--profile tls`, then compare each service in compose's `caddy` and default sets with the rendered unit of the same name. Fields compared:
  - user;
  - read-only flag;
  - tmpfs targets and options (with the Caddy U/notmpcopyup equivalence);
  - dropped and added capabilities;
  - no-new-privileges;
  - init;
  - networks and aliases;
  - volume targets and read-only flags;
  - secret targets, uid, gid and mode;
  - environment keys (values compared only for non-production keys);
  - command/entrypoint;
  - health check command and timings.
- Allowed differences live in one explicit table in the test, each with a one-line reason:
  - ports and restart policy;
  - `GIDMap`, `SecurityLabelType`, `Unmask`;
  - Caddy's command, Caddyfile and environment;
  - production environment values;
  - Caddy's Caddyfile bind, replaced by the image copy.
- A Podman-only test runs `/usr/libexec/podman/quadlet -dryrun -user` against a temporary unit directory (`QUADLET_UNIT_DIRS`) holding the rendered units. It asserts exit 0 and that each generated service's `ExecStart` contains the expected `podman run` options:
  - `--read-only`;
  - `--cap-drop all`;
  - `--security-opt no-new-privileges`;
  - the gid map;
  - the label type;
  - `--unmask`.
- Under Docker that test is not collected, because Docker CI has no Podman. The parity test runs on both engines.

---

## Task 3: `Caddyfile.lan` and image contents

**Goal:** the D8 configuration, shipped in the images (D4).

**Files:**
- Create `deploy/caddy/Caddyfile.lan`.
- Edit `docker/caddy.Dockerfile`: `COPY deploy/caddy/Caddyfile.lan /etc/caddy/Caddyfile.lan`, mode 0444.
- Edit `docker/backend.Dockerfile`: copy `deploy/podman/` to `/usr/share/aegis/podman/`, read-only, with `aegis-podman` mode 0555.
- Tests: extend `tests/deployment/test_compose.py`, or add `test_podman_deploy_caddy.py`.

**`Caddyfile.lan`:**

```
{
	admin off
	auto_https disable_redirects
	skip_install_trust
}
{$AEGIS_LAN_SITES} {
	bind fd/3 {
		protocols h1 h2
	}
	tls internal
	reverse_proxy tls-gateway:8081 {
		header_up X-Forwarded-For {remote_host}
		header_up X-Forwarded-Proto https
	}
}
```

**Tests:**
- `caddy adapt` succeeds in the Caddy image, through the existing `adapted_caddyfile` helper with an environment option, for sample sites `https://aegis.example.test:9443, https://192.0.2.10:9443`. Test values only.
- The adapted JSON:
  - has one server listening on `fd/3`;
  - has protocols `h1` and `h2` and no `h3`;
  - has an internal TLS issuer;
  - reverse-proxies to `tls-gateway:8081` with exactly the two headers from the existing Caddyfiles;
  - has admin disabled.
- The image files exist with the stated modes, checked through the existing image-content tests and a built image.
- Docker compose behavior is unchanged: the existing compose tests still pass.

---

## Task 4: `aegis-podman` installer: configuration, secrets, records, install

**Goal:** D9 and D11, the non-root parts.

**Commands:**
- `init --release <tag> --https-port <p> --name <host> [--name ...] [--address <ip>] [--registry ghcr.io/troja-gnister]`:
  - checks Podman is rootless with SELinux enforcing;
  - checks the port has no listener (socket bind test) and is not claimed by any container's port mapping (`podman ps -a --format json`);
  - pulls the four images by tag and resolves their digests (`podman image inspect --format '{{.Digest}}'` plus repository);
  - creates the eight secret files in `~/.local/share/aegis/secrets/` (directory 0700): 32 random bytes hex-encoded, mode 0600, `chcon -t container_file_t`, then `podman unshare chown 10001:10001`. It refuses if any file already exists unless recorded. Spec D9 explains why Podman `--secret` is not used;
  - chooses a free private /24 for the backend network, colliding with no `podman network inspect` subnet and no host route, and records it;
  - writes `~/.config/aegis/aegis.env` (0600);
  - writes the record `~/.local/share/aegis/state.json` (0600), holding release, digests, prefix, secret names, unit names, port, names and host gid.
- `install`:
  - renders the units into `~/.config/containers/systemd/`, refusing to overwrite any file it did not record;
  - runs `systemctl --user daemon-reload` and `enable` for the socket and services. Quadlet-generated services cannot be enabled, so `WantedBy` takes effect on daemon-reload; document this;
  - records the files and their hashes.
- `start` / `stop` / `status`:
  - start and stop work through `systemctl --user`, on the socket and the services;
  - `status` shows each unit's state, container health, the image digests, and `journalctl --user` hints on failure.
- `upgrade --release <tag>`: pull, resolve digests, re-render, `daemon-reload`, then restart in order (migrate runs first). It refuses a downgrade unless `--allow-downgrade` is given.
- `ca-cert`: prints the Caddy local root certificate from the running Caddy container (`podman exec <prefix>caddy cat /data/caddy/pki/authorities/local/root.crt`).
- `uninstall [--purge-data]`:
  - stops and removes the recorded unit files, then reloads systemd;
  - with `--purge-data`, also removes the recorded volumes (by exact name, after re-checking the volume IDs recorded at creation) and the recorded secret files;
  - never matches by pattern.
- `bootstrap-password`: runs the existing bootstrap-password management command in a one-off container from the backend digest, on the backend network, with the migrator or web secret as the runbook specifies. Read the runbook's bootstrap-password section and mirror it.

**Implementation notes:**
- All external commands go through one runner function, so tests can inject a fake.
- No `shell=True`. No `sudo` anywhere.
- Atomic writes: `O_NOFOLLOW`, `O_EXCL` temporary file, rename. Mode 0600 for config and state; 0644 for unit files.

**Tests:** in `tests/deployment/test_podman_deploy_installer.py`, using a fake runner and `tmp_path` homes:
- argument validation;
- refusal on an existing unrecorded file, secret or volume;
- secrets never appear in argv, logs or state;
- state round-trip;
- `upgrade` digest change and downgrade refusal;
- `uninstall` touches only recorded names;
- a test asserting the installer imports only standard-library modules.

---

## Task 5: Roots, the SELinux module and the host-setup script

**Goal:** D5, D6, D7 and D11 `roots`, reusing `aegisctl mounts`.

**`aegisctl mounts` additions** (backend; tests under `backend/tests/...`, next to the existing mounts tests):
- `render --format quadlet --unit-prefix <prefix> --output-dir <dir>`: writes, per role (gateway, operations, indexer, media), the drop-in `<prefix><role>.container.d/50-aegis-roots.conf`. Each drop-in carries:
  - read-only binds (`Volume=<source>:<target>:ro`, with no SELinux options);
  - the manifest and gateway attestation binds;
  - the worker attestation `Exec=` prefix, exactly as the compose render produces it.
- Import `yaml` lazily, so the quadlet path needs only the standard library.
- Observer for preflight: an engine mode using plain `podman run` with an explicit image reference (the recorded gateway digest), `--gidmap +g101:@<host gid>:1`, `--security-opt label=type:<reader type>` and the existing constrained options, instead of compose with `pull_policy: never`. It keeps the existing observer diagnostics handling and the comma refusal.
- Follow-up (c) from HANDOFF lands here: `mounts validate` refuses commas in sources when the target engine is Podman.

**`aegis-podman roots add --slot <id> --source <path> [--identity <value>]`:**
1. Extract the release's `aegisctl` package from the backend image into `~/.local/share/aegis/releases/<release>/lib/` (`podman create` plus `podman cp`, then remove that exact container).
2. Run `python3 -m aegisctl mounts inspect`, `validate`, `preflight` (Podman observer) and `render --format quadlet` on the host against a private config under `~/.config/aegis/mounts.toml`.
3. Install the drop-ins (recorded) and `daemon-reload`.
4. Detect the SELinux type of the source directory (`os.getxattr(path, "security.selinux")`, parsed). If it is not already covered, regenerate the module (below) and stop with a message telling the user to run the host-setup script. The source must exist and be a directory, and the installer never creates it.
5. Restart the affected units.

`roots remove --slot <id>` reverses this.

**SELinux module (`deploy/podman/selinux/aegis_reader.cil.in`):**

```
(block aegis_reader
    (blockinherit container)
    (blockinherit restricted_net_container)
    @@READ_RULES@@
)
```

- `@@READ_RULES@@` expands, per covered type `T` (validated `^[a-z0-9_]+_t$`), to:
  - `(allow process T (dir (getattr open read search ioctl lock)))`;
  - `(allow process T (file (getattr open read map ioctl lock)))`;
  - `(allow process T (lnk_file (getattr read)))`.
- Process type name: `aegis_reader.process`.
- Check `restricted_net_container` against udica's templates during the host run. If it blocks the gateway's or workers' network use, use `net_container`, and record the reason.

**Host-setup script** (`aegis-podman host-setup` writes it to `~/.local/share/aegis/host-setup.sh`, mode 0700, and prints the path and its contents). It:
- uses `set -eu`;
- checks it runs as root via sudo with `SUDO_USER` equal to the operator;
- installs `udica` if missing (`dnf install -y udica`);
- runs `semodule -i <module> /usr/share/udica/templates/{base_container.cil,net_container.cil}`, with the exact templates used;
- for each operator-named firewalld zone and each private source range, adds a permanent rich rule (`rule family=ipv4|ipv6 source address=<range> port port=<p> protocol=tcp accept`), then runs `firewall-cmd --reload`;
- prints the rules it added.

It never changes zone targets or services and never touches other rules. Zones are `host-setup --zone <z>` arguments, stored in state. The firewall ranges are those in D7.

**Tests:**
- Module rendering, including type validation and refusal of `unconfined_t` and `spc_t`.
- Host-setup script content: exact commands, no zone-target changes, ranges exact.
- `roots add` against the fake runner: refusal of a non-directory source, a missing source, a comma, or a source inside the state directories; state records; drop-in content.
- `aegisctl` quadlet render unit tests mirroring the compose render tests.

---

## Task 6: Deployment harness (`make verify-podman-deploy`)

**Goal:** D12, revised after the Tasks 1–2 review: real systemd semantics with nothing that can start at boot. Run only on Podman hosts.

**Files:**
- `scripts/podman_deploy_check.py`;
- `Makefile` target `verify-podman-deploy`;
- tests of the script's recording and cleanup in `tests/deployment/test_podman_deploy_harness.py` (fake runner).

**Flow:**
1. Preflight checks:
   - the engine is Podman;
   - systemd is 254 or newer;
   - ports 18080 and 55432 and the chosen test socket port are free;
   - at least 40 GiB free on the graph root's filesystem;
   - no file the run would write already exists.
2. Build the four images locally through the existing guarded build path, with run-unique tags. The renderer's test flag allows local image IDs.
3. Create the test secret files (prepared as in Task 4) and a synthetic root (0750/0640, `container_file_t`, in a fresh temporary directory). Choose a free test backend subnet. Render the root drop-ins with `aegisctl mounts render --format quadlet`.
4. Render the units with the test prefix, `install_sections=False`, reader type `container_t` and the test socket port. Record each path, then write them into `~/.config/containers/systemd/` and the socket into `~/.config/systemd/user/`, and run `systemctl --user daemon-reload`.
5. Start `<prefix>caddy.socket` and the services through `systemctl --user start`, and wait for all of them to become active and healthy.
6. Assertions, each reported as `AEGIS_PODMAN_DEPLOY check=<name> status=passed|failed`:
   - healthy through systemd ordering;
   - local-CA HTTPS on the test name, verified with the exported root;
   - per-address login throttling (`127.0.0.1` against the host's non-loopback address, both directions);
   - the indexer attests and reads the synthetic root;
   - a write into the root fails with `EROFS`;
   - no published host port on the gateway;
   - no Podman restart policy on any container;
   - recovery after `systemctl --user restart <prefix>postgres.service`: the backend becomes healthy again without manual steps;
   - a migrate attempt failed on purpose recovers. For example, stop PostgreSQL, start migrate so it fails and retries, then start PostgreSQL and see migrate and its dependents complete.
7. Cleanup on every exit path:
   1. `systemctl --user stop` the recorded units;
   2. remove exactly the recorded unit files, then `daemon-reload`;
   3. remove the recorded containers, networks, volumes, image tags and temporary directories by exact identity;
   4. report any leftover as a failure.

A rerun first cleans up an interrupted run's record.

## Task 7: CI publish job

**Goal:** D3.

**Files:**
- `.github/workflows/ci.yml`;
- `tests/deployment/test_ci_workflow.py` (the existing workflow tests).

**Job:**
- `publish`, with `needs: [backend, frontend, deployment, e2e]` and `if: github.event_name == 'push' && github.ref == 'refs/heads/main'`.
- Permissions: `contents: read`, `packages: write`. Job-level only; the workflow default stays `contents: read`.
- Steps:
  1. Check out the repository (pinned action SHA).
  2. Log in to GHCR with `docker login ghcr.io -u ${{ github.actor }} --password-stdin <<< "$GITHUB_TOKEN"`, or the official login action pinned by SHA.
  3. Build the four images with `docker build` from the tracked Dockerfiles and default build arguments, with labels `org.opencontainers.image.source=https://github.com/${{ github.repository }}` and `org.opencontainers.image.revision=${{ github.sha }}`.
  4. Tag each `ghcr.io/<owner>/aegis-<role>:sha-${{ github.sha }}`, push, and print each pushed digest.
- No `latest` tag. No secrets in logs.

**Tests:** extend the workflow tests:
- the job exists with exactly those `needs`, the `if` condition and the permissions;
- actions are SHA-pinned;
- no other job gains `packages: write`;
- image names and tags follow the pattern.

---

## Task 8: Documentation

**Files:**
- `docs/operations/podman-deployment.md`:
  - prerequisites;
  - bootstrap (D4 commands);
  - `init`, `host-setup` (user runs with sudo), `install`, `start`, trusting the CA on clients, `roots add`, upgrade, uninstall, troubleshooting;
  - generic placeholders only.
- README Podman section: a link.
- `docs/verification/linux-podman-prerequisite.md`: a part 2 section with the measured results (D2 spike, D5 spike, D12 run).
- `HANDOFF.md`: part 2 status.
- `.env.example`: unchanged.
- State that Docker remains primary, and that this guide covers rootless Podman hosts with systemd.

---

## Task 9: Deploy on this host (root session)

Follow spec section 6:
1. Record host-specific values only in ignored notes.
2. Have the user run the host-setup script.
3. Verify from the private network and the VPN, add the first root, rescan, browse.
4. Record evidence in ignored notes, and generic results in the verification report.

The reboot check happens only at a time the user chooses.
