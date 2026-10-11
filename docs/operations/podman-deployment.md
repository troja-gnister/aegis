# Rootless Podman deployment (private network)

**Docker Engine with Compose v2 remains the primary, supported runtime**, and the [Docker deployment runbook](phase-1-deployment.md) is the reference deployment. This guide is optional. It installs Aegis on a rootless Podman host as systemd user (Quadlet) units, from published release images, with no checkout, local build or Compose provider on the server. Aegis is then reachable over HTTPS from the private network and VPN only, through a local certificate authority. The [design](../superpowers/specs/2026-10-10-podman-lan-deployment-design.md) records the decisions; the [verification report](../verification/linux-podman-prerequisite.md#part-2-private-network-deployment-october-2026) records the measurements and acceptance results.

The product boundaries of the Docker runbook apply unchanged: Aegis is an incremental build, not yet a replacement for a working drive or photo library, every original is mounted read-only, and a failed check is a reason to fix deployment inputs, never to grant writes, extra capabilities or a weaker confinement.

## Scope

This guide covers a host with:

- rootless Podman 5.x with Quadlet, under the operator's systemd user manager, systemd 254 or newer;
- SELinux enforcing;
- firewalld;
- `rpm` and `dnf` (a Fedora-family host), which the generated host setup script uses to install udica.

Not covered, each needing its own design and approval: exposure to the internet, ACME certificates, a host reverse proxy, HTTP/3, a port-80 redirect, backup and restore automation (milestone 2G) and multi-host deployment. Putting Aegis on the internet is a separate, optional last step.

Aegis runs beside the host's existing services. The installer never stops, changes or removes an existing container, unit, network, volume, secret or firewall rule.

## Prerequisites

- **Podman 5.x, rootless**, run as a dedicated, non-root operator account. `aegis-podman` refuses to run as root and never calls `sudo`.
- **Linger on** for that account, so its systemd user manager starts at boot and keeps running after logout. Turn it on with `loginctl enable-linger` (polkit normally allows this for your own account; otherwise an administrator runs `sudo loginctl enable-linger <user>`). Check it with `loginctl show-user "$USER" --property=Linger`.
- **systemd 254 or newer.** The units use `RestartSteps=`, `RestartMaxDelaySec=` and `RestartMode=`.
- **Python 3.11 or newer** as `python3`. The installer is a single standard-library script.
- **A free TCP port, 1024 or higher**, for HTTPS (`<port>` below). No privileged port or host sysctl change is used.
- **SELinux enforcing and firewalld.** The host setup script installs udica if it is missing.
- **Names clients use.** Each hostname must resolve to the host for its clients (local DNS, the VPN's DNS or a hosts entry). An IP address needs no DNS; keep it stable, for example with a DHCP reservation.
- **Network access to the registry** for `init` and `upgrade`. If the image packages are not anonymously pullable, run `podman login ghcr.io` with a read-only token first. A boot never contacts a registry.

`init` checks rootless Podman, SELinux enforcing, the systemd version, the Quadlet generator, linger, and that your systemd user manager reads units from the same configuration directory (`XDG_CONFIG_HOME`) the installer writes to. Each refusal names the problem.

## How aegis-podman behaves

- **Ownership.** It creates, changes and removes only what its record (`state.json`) names, by exact path, identity or object name, never by pattern. It refuses to overwrite a file it did not write, or one that changed since it wrote it. It refuses to adopt a container, volume or network that already carries one of its names.
- **Names.** Every unit and Podman object is named `aegis-<name>`: containers such as `aegis-postgres` and `aegis-caddy`, the networks `aegis-backend`, `aegis-edge` and `aegis-tls-hop`, and volumes such as `aegis-postgres-data`. Its volumes and networks carry the label `io.aegis.install=<token>`, unique to the install, and only objects that carry it are ever removed.
- **One command at a time.** Changing commands take an exclusive lock; a second one refuses with `another aegis-podman command is running`.
- **Interruptions.** A command that stops part-way leaves a record its rerun completes: rerun the same command.
- **Privilege.** The SELinux module and the firewall rules go into a script that you review and run with `sudo`. Nothing else needs root.
- **Do not edit what it writes:** unit files, drop-ins, `aegis.env` and `mounts.toml`. Change the deployment through `aegis-podman`.

Commands that wait for systemd (`start`, `stop`, `upgrade`, `roots add`, `roots remove` and `uninstall`) take `--timeout <seconds>` (default 600). Every command also accepts `--prefix` and `--state-home`, which exist for test installations. A production install uses neither.

| Exit code | Meaning |
| --- | --- |
| 0 | Done; for `status`, every unit is active, healthy and on its recorded image. |
| 1 | Refused or failed, or units did not become ready within `--timeout`. The message says why. |
| 2 | Invalid command-line arguments. |
| 3 | `status` only: not installed, stopped or still starting, failed, an upgrade pending, or another command in progress. |
| 4 | `roots add` only: the host setup must load a new SELinux module first (see [Roots](#roots)). |

### Where files go

| Path | Contents |
| --- | --- |
| `~/.config/aegis/aegis.env` | Non-secret configuration (mode 0600), written from the record. |
| `~/.config/aegis/mounts.toml` | The root configuration that `aegisctl` validates, written from the record. |
| `~/.local/share/aegis/state.json` | The record (mode 0600): release, digests, names, port, subnet, roots and every file and object the installer owns. |
| `~/.local/share/aegis/secrets/` | The eight service secrets and, later, the administrator bootstrap password. |
| `~/.local/share/aegis/releases/<release>/` | Each release's installer, templates and `aegisctl`, copied out of its backend image. |
| `~/.local/share/aegis/mounts/` | The root manifest and gateway attestation the drop-ins bind. |
| `~/.local/share/aegis/selinux/aegis_reader.cil` | The rendered SELinux module. |
| `~/.local/share/aegis/host-setup.sh` | The host setup script for you to review and run with `sudo`. |
| `~/.config/containers/systemd/` | The Quadlet units `aegis-*.container`, `.network` and `.volume`, and the root drop-ins `aegis-<role>.container.d/50-aegis-roots.conf`. |
| `~/.config/systemd/user/aegis-caddy.socket` | The HTTPS socket unit. |

The installer's own directories must be real directories owned by you, mode 0700. The configuration directory follows `XDG_CONFIG_HOME` when it is set; the data directory is always `~/.local/share/aegis`.

## Bootstrap

A release is the tag `sha-<commit>`, where `<commit>` is a full 40-hex commit on `main`. CI's `publish` job pushes the four runtime images (`aegis-backend`, `aegis-gateway`, `aegis-postgres` and `aegis-caddy`) for that commit only after the backend, frontend, deployment and e2e jobs pass. It builds all four before pushing any, labels each with the commit and its commit time, and never pushes a `latest` tag. Treat a release tag as immutable.

The installer ships in the backend image. Copy it out:

```sh
podman run --rm ghcr.io/troja-gnister/aegis-backend:sha-<commit> \
  cat /usr/share/aegis/podman/aegis-podman > aegis-podman
```

Then check it before running it:

```sh
podman image inspect --format '{{.Digest}} {{index .Labels "org.opencontainers.image.revision"}}' \
  ghcr.io/troja-gnister/aegis-backend:sha-<commit>
sha256sum aegis-podman
python3 aegis-podman --help
```

- The digest should equal the one the CI `publish` job printed for that commit, and the revision label should equal `<commit>`.
- The checksum should equal that of `deploy/podman/aegis-podman` at `<commit>` in the repository (`git show <commit>:deploy/podman/aegis-podman | sha256sum`).
- It is one readable standard-library Python file; read it.

The examples below run this copy as `python3 aegis-podman`.

**Digest pinning.** `init` and `upgrade` pull the four images by tag, resolve each to its registry digest and record it. The units reference `image@sha256:…`, so a running deployment never follows a moved tag. A release whose tag later resolves to a different digest than the recorded one is refused. Containers use `Pull=never`. For rendering, the installer always uses the installer and templates of the release it installs, copied out of that release's backend image, so the two cannot drift apart. If an older installer copy refuses with `run that release's own aegis-podman`, repeat the bootstrap with the newer tag.

## Initialize

```sh
python3 aegis-podman init --release sha-<commit> --https-port <port> \
  --name <name> --address <lan-ip> --address <vpn-ip>
```

- `--name` is a hostname clients use; repeat it for more. The first `--name` is canonical: it forms the public URL `https://<name>:<port>`. It also accepts an IP address, so a deployment reached only by address can pass `--name <lan-ip>`.
- `--address` adds an IP address clients use; repeat it for more. Direct IP addresses are fine: the local CA issues certificates for IP addresses as well as names.
- Clients must use one of the configured names or addresses with `<port>`; other host names are refused.
- `--registry` selects another registry path holding the same `aegis-<role>:sha-<commit>` images (default `ghcr.io/troja-gnister`).
- `--backend-subnet` gives a free private IPv4 /24 for the internal backend network instead of the first free candidate.

For example, `--name aegis.example.test --address 192.0.2.10` serves `https://aegis.example.test:<port>` and `https://192.0.2.10:<port>`.

**The port and names are fixed at `init`.** This version has no command that changes them later, so choose names and addresses that will stay valid.

`init`:

1. runs the prerequisite checks above;
2. checks that `<port>` has no listener on any address and is not mapped by any container;
3. chooses a private /24 for the backend network that overlaps no Podman network and no host route; web and the gateway get fixed addresses in its lower half, and Podman assigns others from the upper half;
4. pulls the four images and pins their digests;
5. copies the release's installer tree out of the backend image, renders the units in memory, and refuses if any of their names already exists as a container, volume, network or file it did not write;
6. creates the eight service secrets in `~/.local/share/aegis/secrets/`: 32 random bytes each, hex-encoded, never printed, mode 0600, labelled `container_file_t` and handed to container user 10001 with `podman unshare chown`. After that you cannot read them back, and you need not;
7. writes `aegis.env` and the record.

It installs no unit and starts nothing. An interrupted `init` resumes when rerun; once it has finished, it refuses, and `upgrade` or `install` take over.

## Host setup

The gateway, operations, indexer and media containers run as the SELinux type `aegis_reader.process`, which exists only once the `aegis_reader` module is loaded. Run the host setup before the first `start`:

```sh
python3 aegis-podman host-setup --zone <zone>
```

Name each firewalld zone through which the private network or the VPN reaches the host; `firewall-cmd --get-active-zones` lists them. Repeat `--zone` for more. Later runs reuse the recorded zones when `--zone` is omitted. **Never name the zone of an interface attached directly to an ISP or carrier-grade NAT network**, where sources in `10.0.0.0/8` or `100.64.0.0/10` can be other customers.

The command writes `~/.local/share/aegis/selinux/aegis_reader.cil` and `~/.local/share/aegis/host-setup.sh`, and prints both. Review both, then run the script:

```sh
sudo sh ~/.local/share/aegis/host-setup.sh
```

What the script does, in order:

1. `set -eu`: it stops at the first failing command.
2. It refuses unless it runs as root through `sudo` from your account (`SUDO_USER` must be your login).
3. It checks the module file's SHA-256 against the one the script was written for, and stops, changing nothing, if the module changed since; then run `host-setup` again and review both files again.
4. If `rpm -q udica` finds no udica package, it runs `dnf install -y udica`.
5. `semodule -i <module> /usr/share/udica/templates/base_container.cil /usr/share/udica/templates/net_container.cil` installs or replaces the `aegis_reader` module, together with exactly the udica templates it inherits.
6. For each zone, it warns, changing nothing, when the zone's permanent target is `ACCEPT` or the zone already opens `<port>/tcp` to every source: either makes the port reachable from any source.
7. For each zone, each private source range below, and the permanent and then the runtime configuration, it queries the rich rule `rule family=<family> source address=<range> port port=<port> protocol=tcp accept` with `firewall-cmd --query-rich-rule`, adds it with `--add-rich-rule` only when missing, and prints `added` or `already present` for each. Rerunning it is safe.

| Range | Family | Covers |
| --- | --- | --- |
| `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` | IPv4 | Private networks |
| `100.64.0.0/10` | IPv4 | Shared address space, used by VPN overlays |
| `fc00::/7` | IPv6 | Unique local addresses |
| `fe80::/10` | IPv6 | Link-local addresses |

What it never does: reload firewalld (a reload would drop other services' runtime-only rules), change a zone's target, add a service, open a port without a source restriction, touch any other rule, or remove anything. Changing the zone list later adds rules in the new zones; rules in zones no longer named stay until you remove them.

The rules admit; they do not restrict. The socket listens on every address, so any zone whose target accepts everything, or that opens `<port>` another way, admits more than these ranges, including zones you did not name. The script warns only about the zones it is given; check every active zone with `firewall-cmd --zone=<zone> --list-all`.

The module starts with no root type. [Roots](#roots) adds read rules for each root's SELinux type, after which you run `host-setup` and its script again.

## Install, start and status

```sh
python3 aegis-podman install
python3 aegis-podman start
python3 aegis-podman status
```

`install` renders the release's units with that release's own renderer, writes them (mode 0644) into `~/.config/containers/systemd/` and the socket into `~/.config/systemd/user/`, runs `systemctl --user daemon-reload` and enables `aegis-caddy.socket`. Quadlet-generated services cannot be enabled; their `[Install] WantedBy=default.target` takes effect when systemd generates them at each reload. `install` then checks that systemd loaded every unit; if not, `/usr/libexec/podman/quadlet -dryrun -user` shows what Quadlet generates. Recorded roots get their drop-ins back. `install --no-install-sections` renders the units without `[Install]` sections, so nothing starts at boot; `install --install-sections` restores them. The choice is recorded until one of these flags changes it.

`start` starts the socket and the services and waits, up to `--timeout` seconds (default 600), until every unit is active, healthy and running its recorded image. It stops early, printing the status and recovery steps, when a unit fails or a unit it depends on failed. The first start initializes PostgreSQL and runs migrations, so it takes longer. A timeout only ends the wait: systemd keeps starting the units, and `status` shows their progress.

`status` prints the release, the desired run state, the four pinned images, the roots and their SELinux types, any pending upgrade or SELinux type, and one line per unit: its systemd state, last result, restart count, queued job, container health, and whether it runs the recorded image (`current`) or another one (`stale`). It exits 0 only when everything is ready.

Existing containers and units are untouched: `install` writes only `aegis-` files it records and refuses any it did not write.

### Boot behavior

- **systemd user units.** With linger on, your user manager starts at boot. `sockets.target` starts `aegis-caddy.socket`, and `default.target` starts each service.
- **Socket activation.** systemd owns the HTTPS listening socket and passes it to Caddy as file descriptor 3. Connections that arrive while Caddy is starting or restarting wait in the kernel's queue instead of being refused.
- **Health-gated ordering.** PostgreSQL counts as started only once its health check passes (`Notify=healthy`). `aegis-migrate` runs once after it, and web, operations, indexer and media require a successful migration and start only after it. The gateway follows migrations, and Caddy follows the gateway and web.
- **Restart backoff.** Long-running services restart on failure without limit, backing off from 5 seconds to at most one minute (`Restart=on-failure`, `RestartSteps=`, `RestartMaxDelaySec=`, `StartLimitIntervalSec=0`), which mirrors Compose's `unless-stopped`. A failed migration retries in place (`RestartMode=direct`), so the services waiting on it keep waiting instead of failing.
- **No Podman restart policy.** systemd alone starts the containers, so `podman-restart.service` never touches them.
- **No registry at boot.** Containers use `Pull=never` and the pinned digests.

## Trust the local CA on clients

Caddy creates its local certificate authority when it first starts and keeps it in the `aegis-caddy-data` volume. Leaf and intermediate certificates are short-lived and renewed automatically; clients only need to trust the root. Once `start` succeeds, save the root:

```sh
python3 aegis-podman ca-cert > aegis-local-ca.crt
openssl x509 -in aegis-local-ca.crt -noout -subject -fingerprint -sha256
```

The certificate is public, but copy it to devices over a channel you trust, and compare its fingerprint where the device shows it, so nobody substitutes another root. Then open `https://<name>:<port>/` without bypassing any certificate warning.

- **Android:** Settings → Security and privacy → More security settings → Encryption and credentials → Install a certificate → CA certificate, then select the file. Menu names vary by vendor and version. Chrome trusts user-installed CAs; most other apps do not, and Firefox for Android ignores them by default.
- **iOS and iPadOS:** open the file from Safari, Mail or AirDrop, install it under Settings → General → VPN & Device Management, then turn on full trust under Settings → General → About → Certificate Trust Settings.
- **Windows:** open the file, choose Install Certificate, and place it in Trusted Root Certification Authorities; or run `certutil -addstore -f Root aegis-local-ca.crt` from an elevated prompt. Edge and Chrome use this store.
- **macOS:** import the file into the System keychain in Keychain Access and set Trust to Always Trust; or run `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain aegis-local-ca.crt`. Safari and Chrome use it.
- **Linux:** on Fedora-family systems copy it to `/etc/pki/ca-trust/source/anchors/` and run `sudo update-ca-trust`; on Debian-family systems copy it to `/usr/local/share/ca-certificates/` (with a `.crt` name) and run `sudo update-ca-certificates`. Chromium-based browsers that keep their own NSS store need `certutil -d sql:$HOME/.pki/nssdb -A -t C,, -n aegis-local-ca -i aegis-local-ca.crt` (from the NSS tools).
- **Firefox (desktop):** it keeps its own store. Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import, then tick the option to trust it to identify websites.

**Safety:** a device that trusts this root trusts any certificate the CA signs, for any name. Its private key is in the `aegis-caddy-data` volume: protect that volume like a secret, and remove the root from devices you retire. If the volume is removed, Caddy creates a new CA, and every client must trust the new root.

## Administrator bootstrap and root activation

With Aegis started, create the first administrator. Choose the real account name and email first:

```sh
python3 aegis-podman bootstrap-password --username <admin> --email <admin-email>
```

The first run generates a random administrator password and **shows it once**. Save it in a password manager immediately: it is stored only in `~/.local/share/aegis/secrets/bootstrap-password`, mode 0600 and owned by the container user, so you cannot read it back. The command then runs `manage.py bootstrap_admin` in a one-off container from the pinned backend image, on the backend network, with the web unit's user, environment, secrets and confinement; the container is removed afterwards, not database storage. As in the Docker runbook, a matching existing administrator is an idempotent no-op; rerunning bootstrap never changes a password or silently escalates an existing ordinary account. Conflicting attributes fail. Bootstrap is not a password-rotation tool. Open `https://<name>:<port>/admin/`.

**Root activation.** In administration, create the logical root using an already configured slot (one added with `roots add`, below), a display name, and read-only mode. Activate it only after the deployment is ready (`status` exits 0). Slots cannot be created by submitting an arbitrary path in the browser. A missing or invalid manifest prevents activation. Deactivating a root changes metadata and authorization epochs, never its files; deleting root records is disabled. Users, groups and grants work as in the [Docker runbook](phase-1-deployment.md#users-groups-and-grants).

## Roots

The rules of the [Docker runbook's slot section](phase-1-deployment.md#slot-inspection-and-configuration) apply: mount host storage first, roots must not overlap or alias one another, and a root must contain no nested filesystem mounts. The units must be installed first.

```sh
python3 aegis-podman roots add --slot photos --source /path/to/library
```

- `--slot` is the stable slot ID: a lowercase letter, then up to 62 lowercase letters, digits or hyphens.
- `--source` must be an existing directory, given by its real path: no component may be a symbolic link (the refusal names the real path). It must be an absolute, normalized path of printable ASCII without whitespace, quotes, backslashes, commas, colons, `%` or `$`, so that Quadlet, systemd and Podman read it literally. It must not overlap the installer's directories, by path or by device and inode (which also catches a bind-mounted alias), or another root. `aegis-podman` never creates it.
- `--identity` pins the identity you expect, `local:<device>:<inode>` (as `aegisctl mounts inspect` reports it) or `remote:<host>:<path>`. A `local:` identity that differs from the directory's current one is refused, which catches storage that is not mounted. Without it, the current identity is recorded.

What `roots add` does:

1. **SELinux type.** It reads the SELinux type of the source directory itself. It never grants read access to `unconfined_t`, `spc_t`, `*_exec_t`, `shadow*` or `security*` types.
2. **New type: exit code 4.** If the type is new, it adds read rules for it to `aegis_reader.cil` and stops with exit code 4, printing the next steps: review the module, run `aegis-podman host-setup` (the recorded zones are reused), run the script with `sudo`, then rerun the same `roots add` command.
3. **Module probe.** On the rerun, a throwaway container of the pinned backend image runs as `aegis_reader.process`, under the deployment's group mapping, with the root bound read-only and without relabelling. It lists the root's top directory and reads the first byte of one file, within three levels, that its permission bits let it read. This proves without root that the loaded module covers the type (`semodule -l` needs root). If the probe cannot read the root, the command stops with exit code 4 again: either the module is not loaded yet, or the directory is not readable by your group. The probe container is removed; one left by a timeout is removed only by its exact name, label and ID.
4. **Validation.** It copies the release's `aegisctl` out of its backend image and runs `aegisctl mounts inspect`, `validate`, `preflight` (observing the mount from a container of the pinned gateway image, under the same group mapping and SELinux type) and `render --format quadlet`.
5. **Install.** It prepares a new manifest and gateway attestation, installs the drop-ins, reloads systemd, and restarts the units whose drop-ins changed, waiting until they are ready. If Aegis is stopped, the root applies at the next `start`.

Rerunning `roots add` for an existing slot with the same source renders it again, which is also how a failed run recovers. A different source for an existing slot is refused; remove the slot first.

**Read-only guarantees.**

- The root is bound read-only (`:ro`), at `/srv/aegis/roots/<slot>`, into the gateway, operations, indexer and media only. Web and migrate receive only the manifest. Any write fails with `Read-only file system`.
- The `aegis_reader` module grants each covered type directory `getattr open read search ioctl lock`, file `getattr open read map ioctl lock`, and symbolic-link `getattr read`; no write permission. Only those four containers run as `aegis_reader.process`; all others keep `container_t`.
- **Group-only ID mapping.** Each of those containers keeps its non-root user; only its primary group is mapped to your primary host group (`id -g`). The process keeps a subordinate host UID, so it is never the owner of an original: it reads through the group permission bits (as your group) or the bits for others, never the owner bits. Files and directories your group cannot read (for example mode 0600 or 0700) stay unreadable, and the indexer reports them as per-entry permission errors, which fail closed. Supplementary groups are not carried into the containers.
- **Originals are never changed.** There is no relabel (`:z`, `:Z`), no `chown`, `chmod`, ACL change or `label=disable`. The installer grants read access to the root's existing SELinux type instead of changing the root.
- Worker start-up attestation runs as in the Docker deployment and fails closed on any unexpected mount table.

**Removing a root.** Deactivate its logical root in administration first, then:

```sh
python3 aegis-podman roots remove --slot photos
```

The remaining roots are rendered again. Removing the last root removes the drop-ins, manifests, attestation and `mounts.toml`. Changed units restart when Aegis runs. The root's files are untouched. A SELinux type that no remaining root uses is no longer covered, so the next `host-setup` writes a module without it; the loaded module keeps granting it until you run that script.

## Upgrade

Before upgrading, record the running release and take and verify a PostgreSQL backup; `aegis-podman` does not back up, and backup and restore automation is planned in milestone 2G. Then:

```sh
python3 aegis-podman upgrade --release sha-<new-commit>
```

The upgrade runs in two phases:

1. It pulls and pins the new images, copies the new release's installer tree, and renders the units with the new renderer. When roots are configured, it also renders their drop-ins with the new release's `aegisctl`, so a root that cannot be observed stops the upgrade before anything changes. It then records the upgrade as **pending**, writes the units and drop-ins, reloads systemd and, when Aegis runs, restarts the services in order: an unchanged socket keeps listening, and migrations run before the services that need them.
2. Only after that does the new release become current.

If an upgrade stops between the phases, `status` shows it as pending, and most other commands refuse until it is resolved. Rerun `upgrade --release sha-<new-commit>` to finish it, or `upgrade --abandon` to go back: the current release's units and drop-ins are installed again and, when Aegis runs, restarted. Once the restart into the new release was queued, its migrations may have changed the database schema, so `--abandon` is refused unless you also pass `--allow-downgrade`.

**Downgrades.** Releases are ordered by the backend image's commit-time label. `upgrade` refuses an older release, or one that cannot be ordered, unless `--allow-downgrade` is given. **A downgrade does not reverse migrations: restore a matching database backup first**, then rerun with `--allow-downgrade`.

Previous release trees and pulled images are kept. Never use volume removal as an upgrade step.

## Stop, uninstall and purge

```sh
python3 aegis-podman stop
```

`stop` stops the socket and the services and waits until they are inactive. **It does not survive a reboot:** the units stay installed with their `[Install]` sections, so the next boot starts Aegis again. To keep Aegis from starting at boot, run `install --no-install-sections`, or uninstall.

```sh
python3 aegis-podman uninstall
```

`uninstall` stops every recorded unit, disables the socket, removes exactly the recorded unit files and drop-ins, and reloads systemd. A recorded file that changed since the installer wrote it stops the command before anything is removed; `--force-recorded` removes it anyway. Volumes, secrets, roots and the record are kept, so `install` restores the deployment with its data.

```sh
python3 aegis-podman uninstall --purge-data
```

**`--purge-data` permanently deletes the deployment's data**: the PostgreSQL volume (accounts, grants, audit history and the catalog), the other recorded volumes including the local CA, the recorded networks, the secrets, manifests, release trees, configuration and the record. It verifies everything first, and one mismatch removes nothing:

- a volume or network is removed by exact name, and only when it carries this install's `io.aegis.install` label;
- a secret only when its recorded device, inode and change time match;
- a file only when its recorded hash matches, unless `--force-recorded` is given.

Pulled images are kept. Originals are never touched.

Neither form undoes the host setup: the firewall rules and the SELinux module stay. `uninstall --purge-data` prints the commands that would remove every rule for the recorded zones, and the module, for you to review; the installer runs none of them. As root, delete only the rules the script reported as `added`, each in the configuration it reported, so that a rule you had before stays; then remove the module:

```sh
sudo firewall-cmd --permanent --zone=<zone> --remove-rich-rule='rule family=<family> source address=<range> port port=<port> protocol=tcp accept'
sudo firewall-cmd --zone=<zone> --remove-rich-rule='rule family=<family> source address=<range> port port=<port> protocol=tcp accept'
sudo semodule -r aegis_reader
```

Leave the udica template modules (`base_container`, `net_container`) in place unless you know no other policy uses them.

## Secrets

`aegis-podman` has no secret rotation command in this version. A secret file replaced by hand no longer matches its recorded identity: the installer then treats it as foreign, and `uninstall --purge-data` refuses it unless `--force-recorded` is given. The Docker runbook's [credential rules](phase-1-deployment.md#credential-and-session-revocation) still hold: never recreate the database volume to rotate a password.

## Troubleshooting

- **Start with `status`.** For a failed unit it prints the exact recovery commands: read the logs, fix the cause, then clear the failure and start again.

  ```sh
  journalctl --user -u aegis-<service>.service --no-pager -n 200
  systemctl --user reset-failed <units>
  python3 aegis-podman start
  ```

  `Not started, because a unit they need failed` names units that never started because a dependency failed; reset them together with the failed one. Units are `aegis-<service>.service`, `aegis-caddy.socket`, and Quadlet's `aegis-<network>-network.service` and `aegis-<volume>-volume.service`.
- **Units not loaded after `install`.** Run `/usr/libexec/podman/quadlet -dryrun -user` to see Quadlet's generation errors.
- **Exit code 4 from `roots add`.** Follow the printed steps. If the probe still cannot read the root after the host setup, check that the root directory is readable and searchable by your primary group (`ls -ld /path/to/library`), and look for SELinux denials.
- **SELinux denials.** `sudo ausearch -m AVC -ts recent` lists them; reading the audit log needs root. Without root, the journal may hold AVC summaries, depending on the host's logging and on your account's access to the system journal: try `journalctl --since -1h --grep AVC`. The module covers the type of each configured root directory only; entries below a root that carry another type stay unreadable and appear as per-entry permission errors. A denial that needs a new rule is fixed by a narrow rule in a reviewed Aegis release; never work around it with a broader type, `label=disable` or relabelling.
- **Port conflicts.** `init` and the first `install` refuse a port that has a listener or a container port mapping. If another program takes `<port>` later, `aegis-caddy.socket` fails to start, and its journal shows the address in use. Find the holder with `ss -ltnp 'sport = :<port>'` and free the port: this version cannot move Aegis to another port.
- **A Podman update changes the mount table.** A Podman update can change the mount table the containers see (for example, the powercap mask). Worker start-up attestation then fails closed, so the affected services keep restarting without becoming healthy: `status` shows them failing or restarting, and their journal shows the attestation refusal. Do not edit the units, remove `Unmask=` lines or disable attestation to force them up; the remedy is an Aegis release that recognizes the new mount table.
- **`another aegis-podman command is running`.** Wait for it to finish. Meanwhile, `status` prints only the live unit states and exits 3.
- **Temporary files from an interrupted run.** `status` lists them; remove them only while no `aegis-podman` command runs.
- **`linger is off`, or the user manager reads another configuration directory.** Turn linger on, or run `aegis-podman` with the same `XDG_CONFIG_HOME` as your systemd user manager.
- **An existing network `aegis-backend` with another subnet.** Quadlet keeps an existing network as it is, so the fixed addresses would not fit. If the network is Aegis's own, stop Aegis and remove it with `podman network rm aegis-backend`; never remove a network you did not create.

## Security notes

- **Client addresses are preserved.** The kernel accepts each connection on the systemd-owned host socket, so Caddy sees the real client address and forwards it to the gateway over the internal `tls-hop` network, where only Caddy can reach the gateway's TLS listener. Per-client login throttling and the gateway's anti-spoofing check use real addresses. The rootless published-port gap (O11) in the [verification report](../verification/linux-podman-prerequisite.md#o11-exception-list) does not apply to this deployment; it still affects the Compose-based TLS test module under Podman.
- **No published ports.** No unit publishes a port, the gateway included. The only way in is `aegis-caddy.socket`.
- **No public exposure.** The firewall admits `<port>` only from private and VPN source ranges in the zones you name. Internet exposure is a separate, optional step with its own design and approval; this guide does not cover it.
- **Outbound access.** As in the Docker deployment, web, the workers and PostgreSQL stay on internal networks without a route off the host, while the gateway and Caddy join the non-internal `edge` network and can make outbound connections. Strict outbound isolation of the gateway is not provided.
- **Confinement mirrors Compose.** Each service keeps its Compose user, read-only root filesystem, dropped capabilities, no-new-privileges, tmpfs mounts and health checks; a parity test enforces this.
- **Secrets are never printed**, except the generated administrator password, once. Every secret file is mode 0600 and owned by the container user, and the units bind them read-only.
- **The installer never runs anything privileged.** You review and run the host setup script yourself.
