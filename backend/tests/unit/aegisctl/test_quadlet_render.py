from __future__ import annotations

import hashlib
import json
import os
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
import yaml
from aegisctl.cli import main
from aegisctl.mounts import (
    ConfigError,
    local_identity,
    parse_config,
    preflight_slots,
    render_artifacts,
    render_quadlet_artifacts,
    write_manifest,
)

REPOSITORY = Path(__file__).resolve().parents[4]
QUADLET_TEMPLATES = REPOSITORY / "deploy" / "podman" / "quadlet"
PREFIX = "test-aegis-"
DROP_IN = "50-aegis-roots.conf"
ROLES = ("migrate", "web", "operations", "indexer", "media", "gateway")
WORKERS = ("operations", "indexer", "media")
PODMAN_COMMA_REFUSAL = (
    "mount slot photos: Podman cannot bind-mount an original root whose path contains a comma"
)
QUADLET_PATH_REFUSAL = (
    "mount slot photos: a Quadlet original root path must not contain whitespace, "
    "control characters, quotes, backslashes, colons, '%' or '$'"
)


def _fingerprint(index: int) -> str:
    fields = f"0:{32 + index}\0/\0ext4\0/dev/sd{chr(97 + index)}\0".encode()
    return hashlib.sha256(b"aegis.mount-fingerprint.v1\0" + fields).hexdigest()


def _write_config(path: Path, sources: dict[str, Path]) -> None:
    blocks = [
        "[[slots]]\n"
        f"slot_id = {json.dumps(slot_id)}\n"
        f"source = {json.dumps(str(source))}\n"
        f'container_path = "/srv/aegis/roots/{slot_id}"\n'
        'mode = "read_only"\n'
        f"expected_identity = {json.dumps(local_identity(source))}\n"
        for slot_id, source in sources.items()
    ]
    path.write_text("version = 1\n\n" + "\n".join(blocks), encoding="utf-8")


def _fixture(
    tmp_path: Path, names: tuple[str, ...] = ("readonly-private", "second-private"),
    *, manifest_name: str = "manifest.json",
) -> tuple[Path, Path, dict[str, Path]]:
    slot_ids = ("photos", "uploads", "documents")
    sources: dict[str, Path] = {}
    for slot_id, name in zip(slot_ids, names, strict=False):
        source = tmp_path / name
        source.mkdir()
        (source / "keep").write_bytes(b"synthetic original")
        sources[slot_id] = source
    config = tmp_path / "mounts.toml"
    _write_config(config, sources)
    manifest = tmp_path / manifest_name
    validated = tuple(
        replace(slot, mount_fingerprint=_fingerprint(index))
        for index, slot in enumerate(preflight_slots(parse_config(config)))
    )
    write_manifest(manifest, validated, uid=os.geteuid(), gid=os.getegid())
    return config, manifest, sources


@dataclass
class DropIn:
    sections: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    volumes: list[str] = field(default_factory=list)
    exec_lines: list[str] = field(default_factory=list)


def _drop_in(output: Path, role: str) -> DropIn:
    parsed = DropIn()
    text = (output / f"{PREFIX}{role}.container.d" / DROP_IN).read_text(encoding="ascii")
    assert text.endswith("\n")
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            parsed.sections.append(line)
            continue
        key, separator, value = line.partition("=")
        assert separator, line
        if key == "Environment":
            name, assigned, setting = value.partition("=")
            assert assigned and name not in parsed.environment, line
            parsed.environment[name] = setting
        elif key == "Volume":
            parsed.volumes.append(value)
        elif key == "Exec":
            parsed.exec_lines.append(value)
        else:
            raise AssertionError(f"unexpected drop-in key: {line}")
    return parsed


def _template(role: str) -> list[str]:
    return (QUADLET_TEMPLATES / f"{role}.container").read_text(encoding="utf-8").splitlines()


def _render(
    config: Path, manifest: Path, output: Path, attestation: Path, *,
    prefix: str = PREFIX, host_gid: int | None = None,
) -> None:
    render_quadlet_artifacts(
        config, manifest, output, attestation,
        unit_prefix=prefix, host_gid=os.getegid() if host_gid is None else host_gid,
    )


def _tree(path: Path) -> list[str]:
    if not path.exists():
        return []
    return sorted(str(entry.relative_to(path)) for entry in path.rglob("*"))


@pytest.mark.parametrize("slot_count", [1, 2])
def test_quadlet_drop_ins_carry_exactly_what_compose_adds_to_each_role(
    tmp_path: Path, slot_count: int,
) -> None:
    names = ("readonly-private", "second-private")[:slot_count]
    config, manifest, sources = _fixture(tmp_path, names)
    attestation = tmp_path / "gateway.attestation"
    compose_output = tmp_path / "compose.generated.yaml"
    compose_result = render_artifacts(
        config, manifest, compose_output, attestation, uid=os.geteuid(), gid=os.getegid(),
    )
    compose_attestation = attestation.read_bytes()
    output = tmp_path / "units"

    result = render_quadlet_artifacts(
        config, manifest, output, attestation, unit_prefix=PREFIX, host_gid=os.getegid(),
    )

    assert result == compose_result
    assert attestation.read_bytes() == compose_attestation
    assert result.gateway_digest == hashlib.sha256(compose_attestation).hexdigest()
    assert _tree(output) == sorted(
        entry for role in ROLES
        for entry in (f"{PREFIX}{role}.container.d", f"{PREFIX}{role}.container.d/{DROP_IN}")
    )
    services = yaml.safe_load(compose_output.read_bytes())["services"]
    assert set(services) == set(ROLES)
    for role in ROLES:
        service = services[role]
        unit = _drop_in(output, role)
        template = _template(role)
        assert unit.sections == ["[Container]"], role
        # Every compose bind is read-only; Quadlet carries each as a plain ":ro" volume.
        assert all(mount["read_only"] is True for mount in service["volumes"]), role
        assert unit.volumes == [
            f"{mount['source']}:{mount['target']}:ro" for mount in service["volumes"]
        ], role
        # The release ID and process role come from the units themselves.
        provided = set(service["environment"]) - set(unit.environment)
        assert provided <= {"AEGIS_RELEASE_ID", "AEGIS_PROCESS_ROLE"}, role
        assert unit.environment == {
            key: value for key, value in service["environment"].items() if key not in provided
        }, role
        if "AEGIS_RELEASE_ID" in provided:
            assert "Environment=AEGIS_RELEASE_ID=@@RELEASE@@" in template, role
        if "AEGIS_PROCESS_ROLE" in provided:
            assert f"Environment=AEGIS_PROCESS_ROLE={role}" in template, role
        # D5: fixed container users replace compose's operator identity.
        fixed = "101" if role == "gateway" else "10001"
        assert f"User={fixed}" in template and f"Group={fixed}" in template, role
        assert ("user" in service) == (role not in ("migrate", "gateway")), role
        if role in WORKERS:
            assert len(unit.exec_lines) == 1, role
            assert shlex.split(unit.exec_lines[0]) == service["command"], role
            template_exec = [line.removeprefix("Exec=") for line in template
                             if line.startswith("Exec=")]
            assert len(template_exec) == 1, role
            # The drop-in replaces the unit's Exec with the same command behind attestation.
            assert service["command"][2].endswith(f" && exec {template_exec[0]}"), role
        else:
            assert "command" not in service and unit.exec_lines == [], role
    for slot_id, source in sources.items():
        for role in ("gateway", *WORKERS):
            assert f"{source}:/srv/aegis/roots/{slot_id}:ro" in _drop_in(output, role).volumes
        for role in ("migrate", "web"):
            assert all(f"/srv/aegis/roots/{slot_id}" not in volume
                       for volume in _drop_in(output, role).volumes)


def test_quadlet_drop_in_text_is_exact_and_deterministic(tmp_path: Path) -> None:
    config, manifest, sources = _fixture(tmp_path, ("readonly-private",))
    attestation = tmp_path / "gateway.attestation"
    output = tmp_path / "units"

    result = render_quadlet_artifacts(
        config, manifest, output, attestation, unit_prefix=PREFIX, host_gid=os.getegid(),
    )
    first = {role: (output / f"{PREFIX}{role}.container.d" / DROP_IN).read_bytes()
             for role in ROLES}
    again = render_quadlet_artifacts(
        config, manifest, output, attestation, unit_prefix=PREFIX, host_gid=os.getegid(),
    )

    assert again == result
    assert first == {role: (output / f"{PREFIX}{role}.container.d" / DROP_IN).read_bytes()
                     for role in ROLES}
    header = "# Generated by aegisctl mounts render --format quadlet. Do not edit.\n[Container]\n"
    manifest_environment = (
        "Environment=AEGIS_MOUNT_MANIFEST=/run/aegis/mounts.manifest.json\n"
        f"Environment=AEGIS_MOUNT_MANIFEST_SHA256={result.manifest_digest}\n"
    )
    manifest_volume = f"Volume={manifest.resolve()}:/run/aegis/mounts.manifest.json:ro\n"
    root_volume = f"Volume={sources['photos']}:/srv/aegis/roots/photos:ro\n"
    for role in ("migrate", "web"):
        assert first[role].decode("ascii") == header + manifest_environment + manifest_volume
    for role in WORKERS:
        assert first[role].decode("ascii") == (
            header + manifest_environment
            + 'Exec=/bin/sh -ec "aegisctl mounts attest --manifest '
            f"/run/aegis/mounts.manifest.json --role {role} && exec python manage.py run_role "
            f'--role {role}"\n'
            + manifest_volume + root_volume
        )
    assert first["gateway"].decode("ascii") == (
        header
        + "Environment=AEGIS_GATEWAY_MOUNT_ATTESTATION=/run/aegis/mounts.gateway.attestation\n"
        f"Environment=AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256={result.gateway_digest}\n"
        f"Volume={attestation.resolve()}:/run/aegis/mounts.gateway.attestation:ro\n"
        + root_volume
    )
    for role in ROLES:
        path = output / f"{PREFIX}{role}.container.d" / DROP_IN
        assert path.stat().st_mode & 0o777 == 0o644
    assert attestation.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("with,comma", PODMAN_COMMA_REFUSAL),
        ("with%percent", QUADLET_PATH_REFUSAL),
        ("with$dollar", QUADLET_PATH_REFUSAL),
        ("with\nnewline", QUADLET_PATH_REFUSAL),
        ("with\ttab", QUADLET_PATH_REFUSAL),
        ("with space", QUADLET_PATH_REFUSAL),
        ('with"quote', QUADLET_PATH_REFUSAL),
        ("with'quote", QUADLET_PATH_REFUSAL),
        ("with\\backslash", QUADLET_PATH_REFUSAL),
        ("with:colon", QUADLET_PATH_REFUSAL),
    ],
)
def test_quadlet_render_refuses_unit_unsafe_sources_before_any_write(
    tmp_path: Path, name: str, message: str,
) -> None:
    config, manifest, sources = _fixture(tmp_path, (name,))
    attestation = tmp_path / "gateway.attestation"
    output = tmp_path / "units"

    with pytest.raises(ConfigError) as caught:
        _render(config, manifest, output, attestation)

    assert str(caught.value) == message
    assert str(sources["photos"]) not in str(caught.value)
    assert not output.exists() and not attestation.exists()


def test_quadlet_render_refuses_a_source_that_resolves_to_a_unit_unsafe_path(
    tmp_path: Path,
) -> None:
    config, manifest, sources = _fixture(tmp_path, ("plain-link",))
    target = tmp_path / "resolved,elsewhere"
    sources["photos"].rename(target)
    sources["photos"].symlink_to(target, target_is_directory=True)
    output = tmp_path / "units"

    with pytest.raises(ConfigError) as caught:
        _render(config, manifest, output, tmp_path / "gateway.attestation")

    assert str(caught.value) == PODMAN_COMMA_REFUSAL
    assert not output.exists()


@pytest.mark.parametrize("artifact", ["manifest", "attestation"])
@pytest.mark.parametrize("unsafe", ["with%percent", "with$dollar", "with,comma", "with space"])
def test_quadlet_render_refuses_unit_unsafe_artifact_paths(
    tmp_path: Path, artifact: str, unsafe: str,
) -> None:
    manifest_name = f"{unsafe}.json" if artifact == "manifest" else "manifest.json"
    config, manifest, _ = _fixture(tmp_path, ("readonly-private",), manifest_name=manifest_name)
    attestation = tmp_path / (f"{unsafe}.attestation" if artifact == "attestation" else "gw")
    output = tmp_path / "units"

    with pytest.raises(ConfigError, match="Quadlet") as caught:
        _render(config, manifest, output, attestation)

    assert unsafe not in str(caught.value)
    assert not output.exists() and not attestation.exists()


def test_quadlet_render_requires_the_invoking_nonroot_group(tmp_path: Path) -> None:
    config, manifest, _ = _fixture(tmp_path, ("readonly-private",))
    output = tmp_path / "units"
    attestation = tmp_path / "gateway.attestation"

    with pytest.raises(ConfigError, match="invoking group"):
        _render(config, manifest, output, attestation, host_gid=os.getegid() + 1)
    with pytest.raises(ConfigError, match="non-root group"):
        _render(config, manifest, output, attestation, host_gid=0)

    assert not output.exists() and not attestation.exists()


def test_quadlet_render_refuses_root_even_with_a_matching_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, manifest, _ = _fixture(tmp_path, ("readonly-private",))
    monkeypatch.setattr("aegisctl.mounts.os.geteuid", lambda: 0)

    with pytest.raises(ConfigError, match="must not run as root"):
        _render(config, manifest, tmp_path / "units", tmp_path / "gateway.attestation")


@pytest.mark.parametrize(
    "prefix", ["", "aegis", "Aegis-", "-aegis-", "a/b-", "a.b-", "a" * 32 + "-"],
)
def test_quadlet_render_refuses_invalid_unit_prefixes(tmp_path: Path, prefix: str) -> None:
    config, manifest, _ = _fixture(tmp_path, ("readonly-private",))
    output = tmp_path / "units"

    with pytest.raises(ConfigError, match="unit prefix"):
        _render(config, manifest, output, tmp_path / "gateway.attestation", prefix=prefix)

    assert not output.exists()


@pytest.mark.parametrize("kind", ["output-dir", "output-alias", "attestation"])
def test_quadlet_render_refuses_outputs_inside_originals_before_any_write(
    tmp_path: Path, kind: str,
) -> None:
    config, manifest, sources = _fixture(tmp_path, ("readonly-private",))
    source = sources["photos"]
    output = tmp_path / "units"
    attestation = tmp_path / "gateway.attestation"
    if kind == "output-dir":
        output = source / "units"
    elif kind == "output-alias":
        alias = tmp_path / "alias"
        alias.symlink_to(source, target_is_directory=True)
        output = alias / "units"
    else:
        attestation = source / "gateway.attestation"

    with pytest.raises(ConfigError, match="original"):
        _render(config, manifest, output, attestation)

    assert sorted(path.name for path in source.iterdir()) == ["keep"]
    assert (source / "keep").read_bytes() == b"synthetic original"
    assert not (tmp_path / "units").exists() and not (tmp_path / "gateway.attestation").exists()


def test_quadlet_render_refuses_an_attestation_that_aliases_a_drop_in(tmp_path: Path) -> None:
    config, manifest, _ = _fixture(tmp_path, ("readonly-private",))
    output = tmp_path / "units"
    drop_in = output / f"{PREFIX}gateway.container.d" / DROP_IN

    with pytest.raises(ConfigError, match="alias"):
        _render(config, manifest, output, drop_in)

    assert not output.exists()


def test_quadlet_render_refuses_a_manifest_that_no_longer_matches_the_source(
    tmp_path: Path,
) -> None:
    config, manifest, sources = _fixture(tmp_path, ("readonly-private",))
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    sources["photos"].rename(tmp_path / "old")
    replacement.rename(sources["photos"])
    output = tmp_path / "units"

    with pytest.raises(ConfigError, match="photos: source identity changed"):
        _render(config, manifest, output, tmp_path / "gateway.attestation")

    assert not output.exists()


def test_cli_quadlet_render_uses_the_host_group_instead_of_runtime_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config, manifest, _ = _fixture(tmp_path, ("readonly-private",))
    output = tmp_path / "units"
    attestation = tmp_path / "gateway.attestation"
    # The Podman deployment runs fixed container users; AEGIS_UID/AEGIS_GID do not apply.
    monkeypatch.setenv("AEGIS_UID", str(os.geteuid() + 1))
    monkeypatch.setenv("AEGIS_GID", str(os.getegid() + 1))

    assert main([
        "mounts", "render", "--format", "quadlet", "--config", str(config),
        "--manifest", str(manifest), "--unit-prefix", PREFIX, "--output-dir", str(output),
        "--gateway-attestation", str(attestation), "--host-gid", str(os.getegid()),
    ]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "status": "rendered",
        "manifestSha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "gatewayAttestationSha256": hashlib.sha256(attestation.read_bytes()).hexdigest(),
    }
    assert len(_tree(output)) == 2 * len(ROLES)


def test_cli_quadlet_render_reports_a_group_mismatch_as_a_safe_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    config, manifest, _ = _fixture(tmp_path, ("readonly-private",))

    assert main([
        "mounts", "render", "--format", "quadlet", "--config", str(config),
        "--manifest", str(manifest), "--unit-prefix", PREFIX,
        "--output-dir", str(tmp_path / "units"),
        "--gateway-attestation", str(tmp_path / "gateway.attestation"), "--host-gid", "0",
    ]) == 2

    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "error" and "non-root group" in error["message"]


@pytest.mark.parametrize(
    "arguments",
    [
        # compose stays the default and still requires --output.
        ["--gateway-attestation", "gw"],
        ["--output", "o", "--gateway-attestation", "gw", "--unit-prefix", PREFIX],
        ["--output", "o", "--gateway-attestation", "gw", "--host-gid", "1000"],
        ["--output", "o", "--gateway-attestation", "gw", "--host-gid", "0"],
        ["--output", "o", "--gateway-attestation", "gw", "--output-dir", "d"],
        ["--format", "quadlet", "--gateway-attestation", "gw", "--output-dir", "d",
         "--host-gid", "1000"],
        ["--format", "quadlet", "--gateway-attestation", "gw", "--unit-prefix", PREFIX,
         "--host-gid", "1000"],
        ["--format", "quadlet", "--gateway-attestation", "gw", "--unit-prefix", PREFIX,
         "--output-dir", "d"],
        ["--format", "quadlet", "--gateway-attestation", "gw", "--unit-prefix", PREFIX,
         "--output-dir", "d", "--host-gid", "1000", "--output", "o"],
        ["--format", "quadlet", "--gateway-attestation", "gw", "--unit-prefix", PREFIX,
         "--output-dir", "d", "--host-gid", "01000"],
        ["--format", "other", "--output", "o", "--gateway-attestation", "gw"],
    ],
)
def test_cli_render_rejects_options_of_the_other_format(
    arguments: list[str], capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["mounts", "render", "--config", "c", "--manifest", "m", *arguments])

    assert caught.value.code == 2
    assert "aegisctl mounts render: error:" in capsys.readouterr().err
