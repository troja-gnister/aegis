# Rootless Podman private-network deployment design

**Date:** October 10, 2026

**Status:** Part 2 of the Fedora Server continuation. The user delegated routine design decisions on October 10, 2026 ("you are the driver"). Privileged host steps and anything that replaces an existing service still need the user.

**Parent:** [HANDOFF.md](../../../HANDOFF.md); [part 1 design](2026-10-04-podman-dev-environment-design.md); [Linux/Podman report](../../verification/linux-podman-prerequisite.md)

**Baseline:** `main` at `14d8298` (the e2e route-teardown fix that closed the CI timing investigation).

## 1. Outcome and boundary

Aegis runs persistently on a rootless Podman host and is reachable over HTTPS from the private network and VPN only. It starts at boot under the user's systemd manager and indexes a first real, read-only root. The operator installs it from published images: no checkout, local build or compose file is needed on the server. Docker remains the primary, supported runtime, and the Docker compose deployment keeps its exact behavior.

The host's existing services stay untouched. In particular, Aegis runs beside, and does not replace, any existing file service until the user approves that separately.

### Out of scope

- Public internet exposure, ACME certificates, a host reverse proxy, HTTP/3 and a port-80 redirect. Putting Aegis on the internet is an optional last step that needs its own approval.
- Media viewing: downloads, ranges, thumbnails and viewers (part 4).
- Backup and restore automation (milestone 2G), multi-host deployment, and the public documentation sync and Tasks 16–18 (part 3).
- Removing the O11 exception from the compose-based TLS test module under Podman. This design fixes O11 for the deployment; follow-up (j) in section 11 covers the test module.
- Any change to Docker runtime behavior. Shared files may only gain content that Docker ignores.

## 2. Host context

These facts were measured on the development host on October 10, 2026. Host-specific values (names, addresses, ports, zones, paths) stay in ignored local notes, never in tracked files.

- **Engine:** rootless Podman 5.8.7 with crun, netavark and pasta, and SELinux enforcing. Passt ships `pesto`, but the pasta rootless port forwarder needs Podman 6.0, where it is experimental.
- **systemd:** the user manager is running with linger on, and Quadlet is available. Existing containers are started at boot by `podman-restart.service`, which starts only containers that have a restart policy.
- **Ports:** unprivileged ports start at 1024, and several high ports are already taken by existing services.
- **Firewall:** firewalld, with the private network and the VPN in separate zones.
- **First root:** an ext4 tree whose files carry the explicit label `unlabeled_t`. Its directories are owner and group only (`750`), owned by the operator.
- **Measured denials:**
  - A confined container (`container_t`) is denied reading that tree even as container root. This is SELinux.
  - With an SELinux-permitted copy, container UID 10001 is still denied by the permission bits.

## 3. Decisions

### D1. Podman production runs as Quadlet units

On Podman, the production stack is a set of Quadlet units in the user's systemd configuration, not `podman compose`.

Quadlet gives three things compose on Podman cannot:
- it passes systemd sockets to a container (D2);
- systemd orders startup by health, using `Notify=healthy`;
- systemd owns boot start, so containers carry no restart policy and `podman-restart.service` never touches them.

The services, images, users, read-only roots, capabilities, security options, tmpfs mounts, networks, aliases, volumes, secrets and health checks mirror `compose.yaml` one for one. A parity test (D10) enforces this.

Quadlet recreates a container on every restart, so its address can change. nginx pins web's address at start, and web pins the gateway's for proxy trust. So web and the gateway get fixed addresses on the backend network:
- the installer chooses a private /24 that collides with no existing network or route;
- dynamic allocation is limited to the upper half of that /24.

### D2. O11 fix: a socket-activated edge

The user's systemd manager owns the HTTPS listening socket (`aegis-caddy.socket`) and passes it to the Caddy container, which binds it with `bind fd/3`. The kernel accepts each connection on a host socket, so Caddy sees the real client address. Caddy keeps its place in the existing topology: on `edge`, and on the internal `tls-hop` network where only it can reach the gateway's TLS listener. The gateway's trust model is unchanged.

A throwaway spike on October 10 confirmed the approach. Caddy 2.11.4 ran in a rootless container on a bridge network and started through `systemd-socket-activate`. It reported the client's LAN source address, not its own container address.

Caddy's socket binding needs `protocols h1 h2`, because a stream socket cannot carry HTTP/3, and `skip_install_trust`. Site addresses must carry the externally visible port.

Rejected alternatives:
- **Podman 6's pasta forwarder:** experimental and not available in 5.8.7. Revisit it when Podman 6 is adopted.
- **Host networking, or publishing the gateway's TLS listener on loopback:** either would let host-local processes spoof forwarded addresses, which is a trust-model change.
- **Trusting an address range:** forbidden by the deployment runbook.

### D3. Published images

A new `publish` job in CI runs on pushes to `main`, after the backend, frontend, deployment and e2e jobs pass.

- It builds the four runtime images (`aegis-backend`, `aegis-gateway`, `aegis-postgres`, `aegis-caddy`) from the tracked Dockerfiles with their default build arguments (UID and GID 10001).
- It pushes them to `ghcr.io/troja-gnister/aegis-<role>` tagged `sha-<full commit>`, with OCI `source` and `revision` labels.
- Only this job gets `packages: write`. Actions stay pinned by SHA.
- Images carry no secrets or host data. The release identity is the commit SHA.

At install and upgrade time, the installer pulls the four images by tag. It records each image's digest and writes units that reference `image@sha256:…`, so a running deployment never follows a moved tag.

### D4. Everything needed to deploy ships in the images

The backend image carries `/usr/share/aegis/podman/`:
- the host installer `aegis-podman`, a single Python 3 standard-library script;
- the Quadlet templates;
- the SELinux module template (D6);
- the firewall and SELinux host-setup script template (D7).

The Caddy image carries `/etc/caddy/Caddyfile.lan`, the socket-activated local-CA configuration (D8).

Bootstrap on a server needs only Podman and Python 3:

```sh
podman run --rm ghcr.io/troja-gnister/aegis-backend:sha-<commit> \
  cat /usr/share/aegis/podman/aegis-podman > aegis-podman
python3 aegis-podman init --release sha-<commit> ...
```

The installer always takes its templates from the backend image of the release it installs, so the installer and its templates cannot drift apart.

### D5. Originals: group-only ID mapping

Each container that mounts originals (gateway, operations, indexer and media) keeps its normal non-root user. Its primary group is mapped to the operator's host group (`GIDMap=+g<container gid>:@<host gid>:1`).

- The process keeps a subordinate host UID and so is not the owner of any original. It reads through the group permission bits only.
- Read-only binds still refuse every write.
- A spike on October 10 confirmed reads through this mapping and `Read-only file system` on writes.

Rejected alternatives:
- `keep-id`, which makes container processes the host user;
- `:U` or a recursive `chown`, which are forbidden on originals;
- ACLs, which change the originals' metadata.

Files that are not readable by the owning group stay unreadable. The indexer reports them as permission errors, which fail closed per entry.

A service that shares a named volume with an originals reader gets the same group mapping, even without original mounts. Today that adds web, which shares `staging` with operations. This keeps group ownership consistent on shared volumes. PostgreSQL, migrate and Caddy keep the default mapping.

### D6. SELinux: a dedicated, read-only process type

The installer renders a CIL module defining `aegis_reader`. The module inherits udica's base container template and adds read-only rules for the SELinux types of the configured roots:
- directories: `getattr open read search`;
- files: `getattr open read map`;
- symlinks: `getattr read`.

The four containers that mount originals run as `aegis_reader.process`. Every other container keeps `container_t`.

The user installs the module once, with `sudo`, through the host-setup script. Adding a root with a new type regenerates the module.

Rejected alternatives:
- relabeling originals, or `:z`/`:Z` (forbidden);
- `label=disable` (forbidden);
- granting `container_t` itself read access, which would extend the grant to every container on the host.

### D7. Private-network exposure only

- **Socket port:** the operator chooses it at `init`. It must be 1024 or higher (no host sysctl change) and free when checked.
- **Listen address:** the socket listens on all addresses. The firewall decides who may connect.
- **Host-setup script:** the installer generates a script that the user reviews and runs with `sudo`. It:
  - installs `udica` if missing and loads the D6 module;
  - adds firewalld rich rules admitting the HTTPS port only from private sources (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10` for VPN overlays, `fc00::/7` and `fe80::/10`) in the zones the operator names, permanently and at runtime.
- **What the script never does:** change a zone's target, open other ports, or touch other services' rules.
- **Gateway:** publishes no port in this deployment.

### D8. Local-CA HTTPS

`Caddyfile.lan` serves the operator's configured names and optional IP literals on the configured port. It uses `tls internal` with `skip_install_trust`, binds `fd/3` with `protocols h1 h2`, turns `admin` off and disables redirects. Upstream configuration matches the existing Caddyfiles: `reverse_proxy tls-gateway:8081`, with X-Forwarded-For set to `{remote_host}` and X-Forwarded-Proto set to `https`.

Configuration:
- `AEGIS_PUBLIC_URL` is the canonical `https://<name>:<port>`.
- `AEGIS_ALLOWED_HOSTS` lists every configured name.
- Same-origin CSRF works per name, because Django compares Origin with the request host. No CSRF setting changes.

Caddy's CA and certificates persist in the `caddy-data` volume. `aegis-podman ca-cert` prints the root certificate for the operator to install on client devices.

### D9. Secrets and configuration

- **Secrets:** `init` creates the eight secrets as installer-owned files with the existing names in a private directory (0700), each 32 random bytes hex-encoded and never printed.
  - Files are mode 0600, labeled `container_file_t` and owned by container user 10001:10001 (`podman unshare chown`).
  - Units bind them read-only at compose's targets, which matches compose's own file-secret semantics.
  - Podman's `--secret type=mount` is not used, because it mounts secrets read-write, which PostgreSQL's secret staging correctly refuses (measured October 10).
  - Rotation follows the existing runbook: recreate PostgreSQL and its dependents.
- **Non-secret configuration:** kept in `~/.config/aegis/aegis.env` (mode 0600).
- **Installer records:** unit names, image digests, root manifest and secret names live in `~/.local/share/aegis/`.
- **Removal:** `uninstall` removes only the units recorded there. It removes recorded volumes and secrets only with `--purge-data`.

### D10. Parity with compose

A static deployment test, which runs in CI's deployment job and locally on either engine, renders the Quadlet templates with fixed sample values. It compares each service against `docker compose config` of `compose.yaml` on the security-relevant fields.

The only allowed differences are listed in the test, each scoped to exact services and failing when unused:
- ports and restart policy, which are replaced by the socket and systemd (D13);
- dependency conditions, which are replaced by systemd ordering and `Notify=healthy`;
- `GIDMap`, `SecurityLabelType` and `Unmask` (powercap, as in `compose.podman.yaml`);
- static backend addresses (D1);
- image references by digest;
- Caddy's socket binding, `Caddyfile.lan`, profile, sysctl and tmpfs ownership form;
- production environment values.

A second static test runs Podman's Quadlet generator in dry-run mode on the rendered units where Podman is available. Docker CI checks the templates through the parity test only.

### D11. Lifecycle commands

`aegis-podman` (host side, standard library only) provides:
- `init`;
- `install` (render, `daemon-reload`);
- `start`, `stop`, `status`;
- `upgrade --release`;
- `ca-cert`;
- `roots add|remove`;
- `host-setup`;
- `uninstall`.

How the commands behave:
- **Ownership:** it never creates, changes or removes anything it did not record.
- **Root changes:** `roots add` validates the root with the existing `aegisctl mounts` logic, run from the release's backend image under the same ID mapping and SELinux type. It then renders unit drop-ins with read-only binds, the manifest, and attestation as compose has it, regenerates the SELinux module if a new type appears, and restarts the affected units.
- **Startup checks:** worker start-up attestation is unchanged and fails closed.
- **Version guard:** the development-time Podman version guard is not part of production start. Runtime attestation still refuses unexpected mount tables.

### D12. Deployment verification without boot persistence

The Podman deployment test, `make verify-podman-deploy`, runs only on Podman hosts. It exercises real systemd semantics without creating anything that could start at boot.

- **Install:** it renders the units with a unique test prefix, a resource token and no `[Install]` sections. It installs them for the run's duration only, into the user's Quadlet directory, with the socket in the user systemd directory. Then it runs `daemon-reload`.
  - Units without `[Install]` are never enabled, so a reboot during a run starts nothing.
  - The harness records every file it writes and removes exactly those on every exit path.
  - A rerun removes recorded leftovers of an interrupted run.
- **Data:** roots are synthetic and labeled `container_file_t`. Secrets and images are run-unique and recorded.
- **Assert:**
  - all services become healthy through systemd ordering;
  - local-CA HTTPS serves the configured name;
  - two different client source addresses land in different gateway login-throttle buckets (loopback and a non-loopback host address);
  - the indexer reads a group-only (`750`/`640`) synthetic root through the ID mapping;
  - writes to the root fail;
  - a PostgreSQL restart and a failed migrate attempt both recover without manual action;
  - no container has a Podman restart policy.
- **Cleanup:** it deletes exactly the recorded objects. Port, disk and stack-left-running checks match the existing harness rules.

The SELinux module itself is verified on the real host after the user installs it (section 6), because loading policy needs root.

### D13. Restart and readiness semantics

- **Retries:** long-running services retry without limit, with exponential backoff (`Restart=on-failure`, `RestartSteps`, `RestartMaxDelaySec`, `StartLimitIntervalSec=0`). This mirrors compose's `unless-stopped`, and needs systemd 254 or newer.
- **Migrate:** a failure retries with `RestartMode=direct`, so a transient failure does not fail the queued dependents.
- **PostgreSQL readiness:** the health check probes TCP (`pg_isready -h 127.0.0.1`) in both compose and the unit. The entrypoint's reconcile server listens only on the Unix socket, so a socket probe could report ready before clients can connect. This corrects a shared latent race; Docker behavior otherwise stays the same.
- **Image pulls:** containers use `Pull=never`, so a boot never contacts a registry.

## 4. Repository changes

- `deploy/podman/`:
  - Quadlet templates;
  - `aegis-podman`;
  - the SELinux module template;
  - the host-setup template;
  - `Caddyfile.lan` (also copied into the Caddy image).
- Dockerfiles: the backend image copies `deploy/podman/`; the Caddy image copies `Caddyfile.lan`. Nothing else changes.
- `.github/workflows/ci.yml`: the `publish` job.
- `tests/deployment/`: the parity test, the Quadlet dry-run test, installer unit tests (rendering, records, refusals) and the D12 harness.
- `Makefile`: `verify-podman-deploy`.
- Documentation:
  - `docs/operations/podman-deployment.md`, the operator guide with generic placeholders only;
  - a link from the README's Podman section;
  - a Podman deployment section in the verification report;
  - HANDOFF.

## 5. Safety

All part 1 and HANDOFF safety rules apply. These matter most here:
- **Coexistence:** never stop, change or remove an existing container, unit, network, volume, secret or firewall rule. Install units only under recorded `aegis-` names, and never remove anything by name pattern.
- **Test stacks:** never run them at boot. The D12 harness installs no units and gives no restart policy.
- **Originals:** read-only everywhere. No relabel, `chown`, `chmod`, ACL change or `label=disable`.
- **Privileged steps:** the user reviews and runs only the host-setup script. The installer never calls `sudo`.
- **Repository hygiene:** no host-specific names, addresses, ports, zones or paths in tracked files.

## 6. Deployment on this host

This is the acceptance run, done after the code lands and CI publishes images:

1. Run `init`. The ignored local notes record the port and the names reachable from the private network and the VPN.
2. The user runs the generated host-setup script.
3. Run `install` and `start`. All units become healthy.
4. Check that HTTPS answers from the private network and the VPN, using the exported root certificate.
5. Check a client address from outside the host: the gateway throttle sees a non-container address.
6. Add the first root and run a rescan as `root_admin`. The metadata index completes, recording entry counts, duration and peak indexer memory.
7. Browse the root in the UI.
8. A reboot, scheduled with the user, shows that Aegis comes back while the existing services stay unaffected.

## 7. Acceptance

- Docker CI is green, including the new `publish` job and the parity test. Docker compose behavior is unchanged.
- `make verify-podman-deploy` passes on this host. `make verify`, `make verify-compose` (with only the documented O11 module exception) and `make test-e2e` still pass under Podman.
- Section 6 is complete, with the evidence in the verification report and in ignored host notes.

## 8. Risks and open measurements

- **GHCR visibility:** new GHCR packages may start private. Then the user either makes them public or gives the server a read-only token.
- **Quadlet behavior:** health-gated startup (`Notify=healthy`) and `GIDMap` with `@` host IDs are measured in D12, not assumed.
- **SELinux rules:** udica's base template may need extra rules for the gateway (nginx) or Python roles. Denials found on the real host are added as narrow rules, never as a broader type.
- **Podman updates:** a Podman update could change the mount table (the powercap mask). Attestation fails closed, and `status` must say why.
- **Memory:** indexing about 600K entries on real storage is unmeasured here. Record its duration and memory, and do not claim Task 16 acceptance from it.

## 9. Workflow

The approach is subagent-driven development from a written plan:
- one implementer at a time;
- independent review;
- the root session commits.

The root session pushes after local verification and watches CI. Each new Docker-facing change must keep Docker CI green.

## 10. Implementation order

1. Quadlet templates, the parity test and the dry-run test.
2. `Caddyfile.lan` and the socket activation contract.
3. The `aegis-podman` installer with records and refusals.
4. ID mapping, the SELinux module and the host-setup template.
5. The D12 harness (temporary units without `[Install]`) and the Makefile target.
6. Image changes and the CI `publish` job.
7. The operator guide and the verification notes.
8. Deployment on this host (section 6).

## 11. Follow-ups

- **(j)** Run the compose-based TLS test module's Caddy socket-activated under Podman, so its nine O11 tests use real client addresses.
- **(k)** Evaluate Podman 6's pasta port forwarder when the host moves to Podman 6.
