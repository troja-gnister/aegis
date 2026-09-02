from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

from aegisctl.mounts import (
    ConfigError,
    MountAttestationError,
    attest_mounts,
    local_identity,
    parse_config,
    preflight_slots,
    render_artifacts,
    runtime_identity,
    write_manifest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aegisctl", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    mounts = commands.add_parser("mounts", allow_abbrev=False)
    mount_commands = mounts.add_subparsers(dest="mount_command", required=True)

    inspect = mount_commands.add_parser("inspect", allow_abbrev=False)
    inspect.add_argument("--source", type=Path, required=True)

    validate = mount_commands.add_parser("validate", allow_abbrev=False)
    validate.add_argument("--config", type=Path, required=True)

    preflight = mount_commands.add_parser("preflight", allow_abbrev=False)
    preflight.add_argument("--config", type=Path, required=True)
    preflight.add_argument("--manifest", type=Path, required=True)

    render = mount_commands.add_parser("render", allow_abbrev=False)
    render.add_argument("--config", type=Path, required=True)
    render.add_argument("--manifest", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--gateway-attestation", type=Path, required=True)

    attest = mount_commands.add_parser("attest", allow_abbrev=False)
    attest.add_argument("--manifest", type=Path, required=True)
    attest.add_argument("--role", choices=("operations", "indexer", "media"), required=True)
    return parser


def _paths_alias(first: Path, second: Path) -> bool:
    if first.resolve(strict=False) == second.resolve(strict=False):
        return True
    try:
        return first.samefile(second)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ConfigError("output alias cannot be checked") from exc


def _inspect(source: Path) -> int:
    if not source.is_absolute():
        raise ConfigError("inspect source must be absolute")
    try:
        resolved = source.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise ConfigError("inspect source is inaccessible") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigError("inspect source is not a directory")
    if not os.access(resolved, os.R_OK | os.X_OK, effective_ids=True):
        raise ConfigError("inspect source is inaccessible")
    print(local_identity(resolved))
    return 0


def _validate(config: Path) -> int:
    parse_config(config)
    print('{"status":"valid"}')
    return 0


def _preflight(config: Path, manifest: Path) -> int:
    if _paths_alias(config, manifest):
        raise ConfigError("manifest must not alias config")
    uid, gid = runtime_identity()
    slots = parse_config(config)
    validated = preflight_slots(slots)
    digest = write_manifest(manifest, validated, uid=uid, gid=gid)
    print(json.dumps({"status": "preflighted", "manifestSha256": digest}, separators=(",", ":")))
    return 0


def _render(config: Path, manifest: Path, output: Path, gateway_attestation: Path) -> int:
    uid, gid = runtime_identity()
    result = render_artifacts(
        config,
        manifest,
        output,
        gateway_attestation,
        uid=uid,
        gid=gid,
    )
    print(
        json.dumps(
            {
                "status": "rendered",
                "manifestSha256": result.manifest_digest,
                "gatewayAttestationSha256": result.gateway_digest,
            },
            separators=(",", ":"),
        )
    )
    return 0


def _attest(manifest_path: Path, role: str) -> int:
    from aegis_apps.roots.manifest import ManifestError, MountManifest

    digest = os.environ.get("AEGIS_MOUNT_MANIFEST_SHA256", "")
    try:
        manifest = MountManifest.load(manifest_path, digest)
    except ManifestError as exc:
        raise ConfigError(str(exc)) from exc
    if role not in ("operations", "indexer", "media"):
        raise ConfigError("invalid mount role")
    attest_mounts(manifest, role)  # type: ignore[arg-type]
    print('{"status":"attested"}')
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "mounts" and args.mount_command == "inspect":
            return _inspect(args.source)
        if args.command == "mounts" and args.mount_command == "validate":
            return _validate(args.config)
        if args.command == "mounts" and args.mount_command == "preflight":
            return _preflight(args.config, args.manifest)
        if args.command == "mounts" and args.mount_command == "render":
            return _render(args.config, args.manifest, args.output, args.gateway_attestation)
        if args.command == "mounts" and args.mount_command == "attest":
            return _attest(args.manifest, args.role)
    except (ConfigError, MountAttestationError) as exc:
        print(
            json.dumps(
                {"status": "error", "message": str(exc)[:512]},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    raise ConfigError("unsupported command")
