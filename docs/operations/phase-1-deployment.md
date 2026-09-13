# Phase 1 deployment and operation

The [Phase 1 acceptance report](../verification/phase-1.md) records the verified foundation and its limitations. **Supported original roots must contain no nested filesystem mounts.** Host preflight/artifact guards and runtime attestation enforce this restriction; use the separate-root layout below. Do not bypass a rejection or enable writes to originals.

This is a foundation deployment, not yet a replacement for a working drive or photo library. Login, root visibility, administration, audit, jobs, and isolation are implemented; file indexing, viewers, transfers, and document editing are later roadmap work. See [development verification](../development.md) before deploying a changed build.

## Prerequisites

Use Docker with Compose v2 or newer and the locked host tooling from the development guide. Choose a dedicated, non-root host account with read/traverse access to every original root and access to Docker. Phase 1 preflight requires `AEGIS_UID` and `AEGIS_GID` to match that invoking account. Do not run preflight as root or compensate for unreadable roots by adding capabilities.

Backend roles use that UID/GID; the gateway uses `101:101`. Storage sharing/ACLs must permit read/traverse access for both effective container identities. Host preflight alone does not prove gateway access: runtime attestation and readiness must also pass. Do not grant write access to originals to resolve an identity mismatch.

Every original is mounted read-only in gateway/operations/indexer/media; web and migrator receive none. Aegis cannot delete, overwrite, move, or rename original files. Duplicate review is future metadata-only work. No writable original mount is permitted, including when importing a legacy `read_write` declaration. Future edits create separate immutable managed versions, never modifications of originals.

Application processes use unprivileged UIDs, read-only filesystems, dropped capabilities, no Docker socket, and role-scoped secrets. PostgreSQL is the narrow bootstrap exception: its entrypoint starts as UID 0 to stage private secrets and initialize ownership inside its own storage, then runs PostgreSQL as UID 70. It has no original-root mount. Never add the Docker socket or privileged mode to any service.

Outbound network denial applies to web, processing, and database roles on internal networks. Gateway joins external `edge` for host ingress and can make outbound connections while holding read-only original mounts; the optional public TLS front also requires ACME egress. Phase 1 does not provide strict outbound isolation for gateway. That stronger guarantee would require a supported front-proxy or host-firewall design and its own validation. Future optional frontier connectivity remains separate from processing roles.

Use a distinct Compose project for each installation. The commands below use `aegis-local`, not the default project name and not the reserved test project. Before the first start, inspect existing resources and select an unused project name; never point a fresh installation at an existing database volume accidentally.

Create `.env` from [.env.example](../../.env.example) and set a release identity to the exact source commit/build being deployed. Set UID/GID to the host account, not the example defaults. In the same shell used for preflight and Compose:

```bash
export AEGIS_UID="$(id -u)"
export AEGIS_GID="$(id -g)"
export AEGIS_RELEASE_ID="$(git rev-parse HEAD)"
```

For local-only operation set `AEGIS_HTTP_PORT=127.0.0.1:8080`, `AEGIS_PUBLIC_URL=http://localhost:8080`, `AEGIS_ENV=development`, and `DJANGO_SETTINGS_MODULE=aegis.settings.development`. Keep the published database port absent. Do not include `compose.test.yaml` in a real deployment.

In that same shell, explicitly disable TLS profiles for local HTTP (an empty exported value also overrides any profile selection in `.env`):

```bash
export COMPOSE_PROFILES=""
```

For public or cross-device operation configure a real hostname, trusted HTTPS, `AEGIS_ENV=production`, `DJANGO_SETTINGS_MODULE=aegis.settings.production`, exact `AEGIS_PUBLIC_URL`, and `AEGIS_ALLOWED_HOSTS`. Set `AEGIS_TLS_HOST` to that hostname and select the `tls` profile instead:

```bash
export COMPOSE_PROFILES=tls
```

Keep the chosen `COMPOSE_PROFILES` value in the shell for every subsequent Compose command, including recreation, status, logs, upgrades, and shutdown; restore it before operating from a new shell. The public choice starts Caddy with the stack. Keep the gateway's plain HTTP publication on loopback; Caddy uses the dedicated internal TLS hop. Do not expose its internal listener or trust arbitrary forwarding headers.

The `tls-local` profile is a separate local-certificate-authority test deployment, not publicly trusted HTTPS. For that test choice only, export `COMPOSE_PROFILES=tls-local` instead; do not enable both TLS profiles. Public certificate issuance requires working DNS and reachable HTTPS and is not part of the offline acceptance test.

## Secrets

The shipped Compose file reads eight installation-specific secret files under `deploy/secrets/dev/`. Despite the directory name, there are no fixed shared passwords. Before running the generator, ensure any existing secret directory is owned by the invoking account, mode `0700`, and not a symlink; existing files must be regular nonsymlink files owned by that account. The generator does not enforce these prerequisites. Generate the credentials once:

```bash
bash scripts/init-dev-secrets.sh
```

Existing nonempty files are retained, not rotated. Keep the directory private and files mode `0600`, owned by the invoking/runtime account. Do not use symlinks, empty files, inline password environment variables, or command-line passwords. Production can map a protected external secret directory through an explicit Compose override; keep the same role-scoped secret names/targets. Never loosen modes to make an ownership mismatch disappear.

Store a separate strong administrator password in a mode-`0600` file named `deploy/secrets/dev/bootstrap-password`, using a password manager or protected editor. It is not one of the database credentials. Do not print it or put its contents in shell history.

Build the images before mount preflight, which uses the gateway image to observe container mount identities:

```bash
docker compose --project-name aegis-local build web gateway postgres
```

## Slot inspection and configuration

Mount host storage first, including any SMB/NFS filesystem. Record the expected identity with:

```bash
uv run --locked aegisctl mounts inspect --source /mnt/storage/photos
```

Create a private deployment config outside original roots, using [deploy/mounts.example.toml](../../deploy/mounts.example.toml) as the schema. Set the real absolute source, the inspected identity, a stable slot ID, `/srv/aegis/roots/<slot-id>` as its target, and `mode = "read_only"`. Do not copy the example's identity as if it described your storage. Slot IDs are deployment bindings; user-visible names are separate database records. Root sources must not overlap or alias one another.

Phase 1's approved policy requires roots without descendant filesystem mounts. Ordinary subdirectories are allowed, and the selected root may itself be a mountpoint. If `/library/photos` is a separate mount inside `/library`, do not declare `/library`: select `/library/photos` and other non-overlapping roots such as `/library/documents` instead. Declaring both the containing root and its nested mount is not a workaround. Read-only descendants are subject to the same restriction; full nested-mount support is deferred.

Keep host mount topology stable while preflight/render runs. After any mount change, rerun the workflow and recreate affected containers before activating roots. Read-only original permissions must remain intact; a setup error never calls for writable originals or extra container capabilities.

The following commands assume that edited config is `deploy/mounts.local.toml`. Keep it out of version control: it contains host paths. Schema validation does not prove host access or identity:

```bash
uv run --locked aegisctl mounts validate --config deploy/mounts.local.toml
```

## Preflight

```bash
uv run --locked aegisctl mounts preflight --config deploy/mounts.local.toml --manifest deploy/mounts.manifest.json
```

Preflight checks identities, aliasing, nested mounts, and effective read access without writing under any original. Linux and macOS host checks use bounded filesystem metadata; unavailable or unsupported topology fails closed. It observes a mount fingerprint in a constrained disposable container; this supports Docker Desktop, where host/container inode numbers differ. Missing or changed storage fails closed. A manifest is not permission to create the missing source directory.

Keep the manifest, Compose override, gateway attestation, and configured temporary directory outside every original root. Preflight/render reject destinations inside originals, including symlink aliases, detectable Linux bind aliases, and native macOS Data-volume path aliases, before creating generated artifacts. The independent output guard also rejects roots with nested mounts. Runtime attestation rejects nested mounts and checks read/traverse access under each role's effective identity; it does not enumerate or promise access to every descendant.

## Render the override

```bash
uv run --locked aegisctl mounts render --config deploy/mounts.local.toml --manifest deploy/mounts.manifest.json --output compose.mounts.generated.yaml --gateway-attestation deploy/mounts.gateway.attestation
docker compose --project-name aegis-local -f compose.yaml -f compose.mounts.generated.yaml config --quiet
```

The private generated manifest, override, and gateway attestation form one deployment set. Do not edit them manually, share the manifest, or omit the override from later lifecycle commands. Rendering binds the configured user and all role mounts; runtime attestation rejects identity/digest/access drift. Do not disable attestation to force an unavailable root online.

## Compose recreation

```bash
docker compose --project-name aegis-local -f compose.yaml -f compose.mounts.generated.yaml up --build --force-recreate --wait --wait-timeout 180
```

`migrate` runs `deploy_database` as `aegis_migrator`: it verifies deployment prerequisites, applies the migration graph, synchronizes exact role privileges, and records release/schema identity. Web and workers start only after it exits successfully. Do not run migrations as web, substitute a database superuser, or grant workers broad table/schema access. Runtime services cannot synchronize their own privileges.

Changing mounts or the gateway requires recreation of the complete dependent stack, including web. Its current-peer proxy attestation intentionally fails closed if only the gateway moves to a different IP. Do not work around this by trusting an entire network range.

## Readiness

```bash
docker compose --project-name aegis-local -f compose.yaml -f compose.mounts.generated.yaml ps --all
curl --fail http://localhost:8080/health/live
curl --fail http://localhost:8080/health/ready
```

Use the configured HTTPS origin instead of the local URLs in a production deployment; do not bypass certificate validation. Liveness alone is not readiness. Readiness requires PostgreSQL, the expected migration/schema state, mount identity, and fresh non-stopping heartbeats from operations, indexer, and media for the same release. A completed `migrate` container is expected; other core services must be healthy.

## Administrator bootstrap

```bash
docker compose --project-name aegis-local -f compose.yaml -f compose.mounts.generated.yaml run --rm --no-deps --volume "$PWD/deploy/secrets/dev/bootstrap-password:/run/secrets/bootstrap-password:ro" web python manage.py bootstrap_admin --username admin --email admin@example.invalid --password-file /run/secrets/bootstrap-password
```

Choose the real account name/email before running. The password is read only from the protected file. A matching existing administrator is an idempotent no-op; rerunning bootstrap never changes a password or silently escalates an existing ordinary account. Conflicting attributes fail. The one-off container is removed, not database storage. Open `/admin/` at the configured origin.

## Root activation

In administration, create the logical root using an already configured slot, a display name, and read-only mode. Activate it only after the deployment above is ready. Slots cannot be created by submitting an arbitrary path in the browser. A missing/invalid manifest prevents activation. Deactivating a root changes metadata and authorization epochs, never its files; deleting root records is disabled.

## Users, groups, and grants

Create accounts and groups in administration. A staff/superuser flag grants administration privileges, not product-root access. Add a root grant with exactly one user or group principal. For the Phase 1 root shell, permissions `1` means `BROWSE`; grant only what is needed. Other permission bits reserve later capabilities and do not make unimplemented operations available or originals writable.

Direct and group grants are additive with no deny rule. To remove access, remove every applicable grant or group membership, or deactivate the account/root. Removing a grant deletes only the authorization record, never file content. Confirm each user's view through `/login` and `/roots`; an ungranted administrator should see no roots.

## Credential and session revocation

Sign out closes private UI immediately and requests server-session revocation. If confirmation fails, the public login screen says so and remains closed until explicit credential login. Sessions also expire after 30 minutes idle or 12 hours absolute age. Authorization changes invalidate affected epochs and stale sessions/jobs.

For an account incident, deactivate it using the user administration action. Reactivation permits a new login but does not restore old sessions. Keep a separate administrator available before disabling the current operator. Phase 1 intentionally does not expose browser password-change/reset flows; bootstrap is not a password-rotation tool. Do not use raw SQL or an unaudited password change as a recovery shortcut.

Each PostgreSQL restart reconciles the five managed login credentials from their private files before accepting network clients. To rotate one, replace its file securely and recreate PostgreSQL and dependent roles together during a maintenance window. Changing `postgres-superuser-password` alone does **not** rotate the superuser password on an existing cluster; that credential requires a separate database-administrator procedure. Never recreate the database volume to rotate a password.

## Audit and status

The read-only audit administration view records authentication, identity, root, and grant events. Application and database boundaries reject audit updates/deletes. Do not grant runtime logins ownership or maintenance privileges.

An authenticated staff account can inspect `/api/v1/admin/operations/status` for bounded role state, job counts, queue age, and available progress/pressure metrics. Missing scan progress and unknown disk pressure are normal before the later processing features exist. Status endpoints never return host paths or file content.

Use bounded service logs locally; do not publish raw output:

```bash
docker compose --project-name aegis-local -f compose.yaml -f compose.mounts.generated.yaml logs --no-color --tail 100 web operations indexer media gateway
```

A mount or authorization failure is a reason to fix deployment inputs, not to grant writable originals or bypass identity checks. Preserve opaque request IDs when investigating, without credentials or paths.

## Upgrade checkpoint

Before changing versions, record the running commit/image identities, deployment project, protected secret sources, manifest identity, and PostgreSQL backup. Stop external writes when a consistent snapshot requires it. Preserve the original filesystem independently: the database does not contain original bytes, and originals alone do not restore accounts, grants, or audit history. Automated backup/restore and recovery drills remain Phase 6 work.

Use a reviewed, locked build; rerun preflight/render when mount inputs change, then recreate the full deployment and verify readiness and representative user access. Do not assume downgrading an image reverses migrations. Keep the previous checkpoint until upgrade verification succeeds. Never use volume removal as an upgrade step.

## Safe shutdown

```bash
docker compose --project-name aegis-local -f compose.yaml -f compose.mounts.generated.yaml down
```

This removes the deployment's containers and networks but retains named volumes, including PostgreSQL and TLS state. Do not add `--volumes`, run a broad volume prune, or target another project's resources. Original roots remain unchanged and usable outside Aegis. Keep protected secrets, config, manifest, and named volumes for restart. The volume-deleting teardown inside the isolated browser-test script is exclusively for its disposable test project and must not be copied here.
