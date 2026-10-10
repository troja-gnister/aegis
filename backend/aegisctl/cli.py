from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

from aegisctl.mounts import (
    ConfigError,
    MountAttestationError,
    attest_mounts,
    ensure_outputs_outside_originals,
    local_identity,
    observe_mount_fingerprints,
    observe_mount_fingerprints_podman_run,
    parse_config,
    preflight_slots,
    refuse_podman_comma_sources,
    render_artifacts,
    render_quadlet_artifacts,
    require_host_group,
    runtime_identity,
    write_manifest,
)

# Options that belong to one --observer (preflight) or --format (render) choice only.
_PREFLIGHT_OPTIONS = {
    "compose": ((), ("observer_image", "host_gid", "reader_type", "allow_test_image_id")),
    "podman-run": (("observer_image", "host_gid", "reader_type"), ()),
}
_RENDER_OPTIONS = {
    "compose": (("output",), ("unit_prefix", "output_dir", "host_gid")),
    "quadlet": (("unit_prefix", "output_dir", "host_gid"), ("output",)),
}


def _group_id(value: str) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]{0,9}", value):
        raise argparse.ArgumentTypeError("invalid group ID")
    return int(value)


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
    preflight.add_argument("--observer", choices=tuple(_PREFLIGHT_OPTIONS), default="compose")
    preflight.add_argument("--observer-image")
    preflight.add_argument("--host-gid", type=_group_id)
    preflight.add_argument("--reader-type")
    # Deployment tests only: a local image ID stands in for a release digest.
    preflight.add_argument("--allow-test-image-id", action="store_true")
    preflight.set_defaults(usage_error=preflight.error, choice="observer",
                           choice_options=_PREFLIGHT_OPTIONS)

    render = mount_commands.add_parser("render", allow_abbrev=False)
    render.add_argument("--config", type=Path, required=True)
    render.add_argument("--manifest", type=Path, required=True)
    render.add_argument("--format", choices=tuple(_RENDER_OPTIONS), default="compose")
    render.add_argument("--output", type=Path)
    render.add_argument("--gateway-attestation", type=Path, required=True)
    render.add_argument("--unit-prefix")
    render.add_argument("--output-dir", type=Path)
    render.add_argument("--host-gid", type=_group_id)
    render.set_defaults(usage_error=render.error, choice="format", choice_options=_RENDER_OPTIONS)

    attest = mount_commands.add_parser("attest", allow_abbrev=False)
    attest.add_argument("--manifest", type=Path, required=True)
    attest.add_argument("--role", choices=("operations", "indexer", "media"), required=True)
    return parser


def _check_choice_options(args: argparse.Namespace) -> None:
    """Report another choice's options as argparse usage errors, before any work."""
    if not hasattr(args, "choice_options"):
        return
    selected = getattr(args, args.choice)
    required, forbidden = args.choice_options[selected]
    missing = [name for name in required if getattr(args, name) is None]
    if missing:
        args.usage_error(
            "the following arguments are required: "
            + ", ".join(_flag(name) for name in missing)
        )
    for name in forbidden:
        value = getattr(args, name)
        # Identity checks: a group ID of 0 is still a supplied option.
        if value is not None and value is not False:
            args.usage_error(f"argument {_flag(name)}: not allowed with --{args.choice} {selected}")


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


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
    # Still schema-only: Podman's comma refusal needs no provider, socket or source access.
    refuse_podman_comma_sources(parse_config(config))
    print('{"status":"valid"}')
    return 0


def _preflight(args: argparse.Namespace) -> int:
    config: Path = args.config
    manifest: Path = args.manifest
    if _paths_alias(config, manifest):
        raise ConfigError("manifest must not alias config")
    podman_run = args.observer == "podman-run"
    if podman_run:
        # The Podman deployment runs fixed container users with the operator's group
        # mapped (D5), so the compose runtime identity does not apply to it.
        uid, gid = os.geteuid(), require_host_group(args.host_gid)
    else:
        uid, gid = runtime_identity()
    slots = parse_config(config)
    ensure_outputs_outside_originals((manifest,), tuple(slot.source for slot in slots))
    if podman_run:
        validated = observe_mount_fingerprints_podman_run(
            preflight_slots(slots),
            image=args.observer_image,
            host_gid=gid,
            reader_type=args.reader_type,
            allow_test_image_id=args.allow_test_image_id,
        )
    else:
        validated = observe_mount_fingerprints(preflight_slots(slots))
    digest = write_manifest(manifest, validated, uid=uid, gid=gid)
    print(json.dumps({"status": "preflighted", "manifestSha256": digest}, separators=(",", ":")))
    return 0


def _render(args: argparse.Namespace) -> int:
    if args.format == "quadlet":
        result = render_quadlet_artifacts(
            args.config,
            args.manifest,
            args.output_dir,
            args.gateway_attestation,
            unit_prefix=args.unit_prefix,
            host_gid=args.host_gid,
        )
    else:
        uid, gid = runtime_identity()
        result = render_artifacts(
            args.config,
            args.manifest,
            args.output,
            args.gateway_attestation,
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
    _check_choice_options(args)
    try:
        if args.command == "mounts" and args.mount_command == "inspect":
            return _inspect(args.source)
        if args.command == "mounts" and args.mount_command == "validate":
            return _validate(args.config)
        if args.command == "mounts" and args.mount_command == "preflight":
            return _preflight(args)
        if args.command == "mounts" and args.mount_command == "render":
            return _render(args)
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
