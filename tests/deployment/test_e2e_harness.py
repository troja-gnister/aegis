from __future__ import annotations

import json
import os
import runpy
import stat
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml
from aegisctl.container_resources import (
    ProjectInventory,
    ProjectResource,
    ProjectResourceError,
    admit_project_transition,
)

SUPPORT = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/e2e_support.py"))


def test_failure_logs_never_retain_unstructured_messages_or_credentials() -> None:
    for raw in (
        'password=private-sentinel /srv/aegis/roots/personal',
        json.dumps({"level": "ERROR", "status": 503, "message": "private-sentinel",
                    "metadata": {"password": "private-sentinel"}}),
        json.dumps({"level": {"secret": "private-sentinel"}, "status": "private-sentinel"}),
        "private-sentinel" * 8000,
    ):
        result = json.loads(SUPPORT["sanitize_line"](raw))
        assert "private-sentinel" not in json.dumps(result)
        assert "/srv/aegis" not in json.dumps(result)
        assert set(result) <= {"level", "status", "message"}


def test_generated_secrets_are_private_unique_and_cleanup_preserves_unknown_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_path
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    path = private_e2e_directory()
    SUPPORT["prepare"](path)
    files = list((path / "secrets").iterdir())
    assert len(files) == 11
    assert len({file.read_text() for file in files}) == len(files)
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in files)
    assert (path / "roots/alice").is_dir()
    assert (path / "roots/bob").is_dir()
    mounts = (path / "mounts.toml").read_text(encoding="ascii")
    assert str(path / "roots/alice") in mounts
    assert str(path / "roots/bob") in mounts
    assert str(Path(__file__).resolve().parents[2] / "tests/fixtures/roots") not in mounts
    sentinel = path / "unrecognized-data"
    sentinel.write_text("retain")
    with pytest.raises(ValueError, match="unknown"):
        SUPPORT["cleanup"](path)
    assert sentinel.read_text() == "retain"
    assert (path / "secrets/e2e-alice-password").exists()
    sentinel.unlink()
    SUPPORT["cleanup"](path)


def test_prepared_roots_are_gateway_readable_regardless_of_process_umask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    previous_umask = os.umask(0o077)
    try:
        path = private_e2e_directory()
        try:
            SUPPORT["prepare"](path)
            for name in SUPPORT["ROOT_NAMES"]:
                assert (path / "roots" / name).stat().st_mode & 0o777 == 0o755
            # Unaffected: parent directories and secrets keep their restrictive modes.
            assert path.stat().st_mode & 0o777 == 0o700
            assert (path / "roots").stat().st_mode & 0o777 == 0o700
            assert (path / "secrets").stat().st_mode & 0o777 == 0o700
            for secret in (path / "secrets").iterdir():
                assert secret.stat().st_mode & 0o777 == 0o600
        finally:
            SUPPORT["cleanup"](path)
    finally:
        os.umask(previous_umask)


@pytest.mark.parametrize("directory", ("/", "/tmp", "/Users", "/srv/aegis"))
def test_cleanup_refuses_broad_or_unowned_directory_targets(directory: str) -> None:
    with pytest.raises((ValueError, OSError)):
        SUPPORT["checked_directory"](directory)


def _safe_compose() -> dict:
    services = {}
    for role in ("web", "migrate", "operations", "indexer", "media", "gateway"):
        services[role] = {
            "read_only": True, "cap_drop": ["ALL"], "networks": {"backend": {}},
            "volumes": [] if role in ("web", "migrate") else [
                {"type": "bind", "source": f"/fixture/{name}",
                 "target": f"/srv/aegis/roots/e2e-{name}", "read_only": True}
                for name in ("alice", "bob")
            ],
        }
    services["gateway"]["ports"] = [{"host_ip": "127.0.0.1"}]
    services["postgres"] = {"ports": [{"host_ip": "127.0.0.1"}]}
    return {
        "name": "aegis-phase1-e2e", "services": services,
        "networks": {"backend": {"internal": True}},
        "volumes": {"postgres-data": {"name": "aegis-phase1-e2e_postgres-data"}},
    }


@pytest.mark.parametrize("drift", ("protected_volume", "external", "writable", "egress", "port"))
def test_compose_gate_rejects_unsafe_resource_or_original_mount_changes(drift: str) -> None:
    original = _safe_compose()
    SUPPORT["check_compose"](original)
    changed = deepcopy(original)
    if drift == "protected_volume":
        changed["volumes"]["postgres-data"]["name"] = "aegis_postgres-data"
    elif drift == "external":
        changed["volumes"]["postgres-data"]["external"] = True
    elif drift == "writable":
        changed["services"]["operations"]["volumes"][0]["read_only"] = False
    elif drift == "egress":
        changed["services"]["indexer"]["networks"]["edge"] = {}
    else:
        changed["services"]["postgres"]["ports"][0]["host_ip"] = "0.0.0.0"
    with pytest.raises(ValueError):
        SUPPORT["check_compose"](changed)


def test_test_profile_keeps_fixture_credentials_out_of_the_production_compose() -> None:
    repository = Path(__file__).resolve().parents[2]
    assert "e2e-alice-password" not in (repository / "compose.yaml").read_text()
    assert "e2e-alice-password" in (repository / "compose.test.yaml").read_text()
    assert os.access(repository / "scripts/test-e2e.sh", os.X_OK)


def private_e2e_directory() -> Path:
    path = Path(tempfile.mkdtemp(prefix="aegis-phase1-e2e.", dir="/tmp"))
    path.chmod(0o700)
    return path


def complete_runtime_inputs(path: Path) -> None:
    for name in SUPPORT["GENERATED_NAMES"]:
        target = path / name
        if not target.exists():
            target.write_text("generated\n", encoding="ascii")
    SUPPORT["record_generated"](path)


def test_podman_preparation_targets_only_inventoried_synthetic_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = private_e2e_directory()
    provider = tmp_path / "provider"
    provider.write_text("provider", encoding="ascii")
    provider.chmod(0o700)
    commands: list[list[str]] = []

    def completed(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(arguments)
        output = ""
        if arguments[2:4] == ["unshare", "stat"]:
            output = "1000:1000\n" * (len(SUPPORT["SECRET_NAMES"]) + 1)
        return subprocess.CompletedProcess(arguments, 0, output, "")

    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        support_globals = SUPPORT["prepare_runtime"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setitem(
            support_globals, "container_command",
            lambda *arguments: ["podman", "--remote=false", *arguments],
        )
        monkeypatch.setenv("AEGIS_UID", "1000")
        monkeypatch.setenv("AEGIS_GID", "1000")
        monkeypatch.setattr(subprocess, "run", completed)

        SUPPORT["prepare_sources"](path)
        complete_runtime_inputs(path)
        SUPPORT["prepare_runtime"](path)

        secrets = [str(path / "secrets" / name) for name in SUPPORT["SECRET_NAMES"]]
        label_targets = [
            *secrets,
            str(path / "mounts.manifest.json"),
            str(path / "mounts.gateway.attestation"),
        ]
        # Every recorded entry of the two mounted source roots; never the unmounted sibling.
        mounted = sorted(
            name for name in SUPPORT["_load_ledger"](path)["entries"]
            if name.startswith(("roots/alice/", "roots/bob/")) or name in (
                "roots/alice", "roots/bob",
            )
        )
        assert len(mounted) > 600 and "roots/alice/link-outside" in mounted
        assert commands == [
            ["chcon", "--no-dereference", "--type", "container_file_t", "--",
             *(str(path / name) for name in mounted)],
            ["chcon", "--no-dereference", "--type", "container_file_t", "--", *label_targets],
            ["podman", "--remote=false", "unshare", "chown", "--no-dereference", "1000:1000", "--",
             *secrets, str(path / "mounts.manifest.json")],
            ["podman", "--remote=false", "unshare", "stat", "--format=%u:%g", "--",
             *secrets, str(path / "mounts.manifest.json")],
        ]
    finally:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["cleanup"](path)


def test_e2e_mapping_refuses_added_entry_without_updating_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = private_e2e_directory()
    unknown = path / "added-during-map"
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        complete_runtime_inputs(path)
        ledger_path = path / SUPPORT["LEDGER_NAME"]
        original_ledger = ledger_path.read_bytes()
        support_globals = SUPPORT["prepare_runtime"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setitem(
            support_globals, "container_command",
            lambda *arguments: ["podman", "--remote=false", *arguments],
        )
        monkeypatch.setenv("AEGIS_UID", "1000")
        monkeypatch.setenv("AEGIS_GID", "1000")

        def completed(
            arguments: list[str], **kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            del kwargs
            if arguments[2:4] == ["unshare", "chown"]:
                unknown.write_text("retain\n", encoding="ascii")
            output = ""
            if arguments[2:4] == ["unshare", "stat"]:
                output = "1000:1000\n" * (len(SUPPORT["SECRET_NAMES"]) + 1)
            return subprocess.CompletedProcess(arguments, 0, output, "")

        monkeypatch.setattr(subprocess, "run", completed)
        with pytest.raises(ValueError, match="ownership inventory changed"):
            SUPPORT["prepare_runtime"](path)
        assert ledger_path.read_bytes() == original_ledger
        assert unknown.read_text(encoding="ascii") == "retain\n"
    finally:
        unknown.unlink(missing_ok=True)
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["cleanup"](path)


@pytest.mark.parametrize("field", (stat.ST_UID, stat.ST_GID))
def test_e2e_preparation_refuses_recorded_owner_drift_before_mutation(
    monkeypatch: pytest.MonkeyPatch, field: int,
) -> None:
    path = private_e2e_directory()
    commands: list[list[str]] = []
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        complete_runtime_inputs(path)
        target = path / "mounts.manifest.json"
        original_lstat = Path.lstat

        def changed(candidate: Path) -> os.stat_result:
            metadata = original_lstat(candidate)
            if candidate == target:
                values = list(metadata)
                values[field] += 1
                return os.stat_result(values)
            return metadata

        support_globals = SUPPORT["prepare_runtime"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setattr(Path, "lstat", changed)
        monkeypatch.setattr(
            subprocess, "run", lambda arguments, **kwargs: commands.append(arguments),
        )

        with pytest.raises(ValueError, match="inventory changed"):
            SUPPORT["prepare_runtime"](path)
        assert commands == []
    finally:
        monkeypatch.setattr(Path, "lstat", original_lstat)
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["cleanup"](path)


def test_podman_preparation_refuses_replaced_or_unknown_inputs_before_ownership_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = private_e2e_directory()
    provider = tmp_path / "provider"
    provider.write_text("provider", encoding="ascii")
    provider.chmod(0o700)
    ownership_called = False
    original_manifest = path / "original-manifest"

    def replace_manifest(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal ownership_called
        del kwargs
        if arguments[:4] == ["podman", "--remote=false", "unshare", "chown"]:
            ownership_called = True
        else:
            manifest = path / "mounts.manifest.json"
            manifest.rename(original_manifest)
            manifest.symlink_to(path / "mounts.toml")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        complete_runtime_inputs(path)
        support_globals = SUPPORT["prepare_runtime"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setitem(
            support_globals, "container_command",
            lambda *arguments: ["podman", "--remote=false", *arguments],
        )
        monkeypatch.setenv("AEGIS_UID", "1000")
        monkeypatch.setenv("AEGIS_GID", "1000")
        monkeypatch.setattr(subprocess, "run", replace_manifest)

        with pytest.raises(ValueError, match=r"inventory|replaced"):
            SUPPORT["prepare_runtime"](path)
        assert ownership_called is False

        (path / "mounts.manifest.json").unlink()
        original_manifest.rename(path / "mounts.manifest.json")
        (path / "unexpected").write_text("retain\n", encoding="ascii")
        with pytest.raises(ValueError, match="unknown"):
            SUPPORT["prepare_runtime"](path)
        with pytest.raises(ValueError, match="unknown"):
            SUPPORT["cleanup"](path)
        assert (path / "secrets/e2e-alice-password").exists()
    finally:
        (path / "unexpected").unlink(missing_ok=True)
        if original_manifest.exists():
            (path / "mounts.manifest.json").unlink(missing_ok=True)
            original_manifest.rename(path / "mounts.manifest.json")
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["cleanup"](path)


def test_e2e_ledger_refuses_intermediate_symlink_hardlink_and_known_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = private_e2e_directory()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "alice").mkdir()
    (outside / "bob").mkdir()
    commands: list[list[str]] = []
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        roots = path / "roots"
        saved = path / "saved-roots"
        roots.rename(saved)
        roots.symlink_to(outside, target_is_directory=True)
        support_globals = SUPPORT["prepare_sources"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setattr(
            subprocess, "run", lambda arguments, **kwargs: commands.append(arguments),
        )
        with pytest.raises(ValueError, match=r"inventory|replaced"):
            SUPPORT["prepare_sources"](path)
        assert commands == []
        roots.unlink()
        saved.rename(roots)

        original = tmp_path / "user-original"
        original.write_text("preserve", encoding="ascii")
        linked = path / "unexpected-hardlink"
        os.link(original, linked)
        with pytest.raises(ValueError, match="hardlink"):
            SUPPORT["cleanup"](path)
        assert original.read_text(encoding="ascii") == "preserve"
        assert (path / "secrets/e2e-alice-password").exists()
        linked.unlink()
    finally:
        if path.exists():
            monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
            SUPPORT["cleanup"](path)


def test_e2e_resource_cleanup_refusal_preserves_complete_filesystem_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = private_e2e_directory()
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        cleanup_globals = SUPPORT["resources_cleanup"].__globals__

        def refuse(_inventory: object) -> None:
            raise RuntimeError("unknown project resource")

        monkeypatch.setitem(cleanup_globals, "cleanup_project_inventory", refuse)
        with pytest.raises(RuntimeError, match="unknown project resource"):
            SUPPORT["resources_cleanup"](path)
        assert (path / "secrets/e2e-alice-password").exists()
        assert (path / "roots/alice").is_dir()
        assert (path / SUPPORT["LEDGER_NAME"]).exists()
    finally:
        SUPPORT["cleanup"](path)


def test_e2e_resource_record_refuses_unknown_addition_without_overwriting_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = private_e2e_directory()
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        resource = ProjectResource(
            "container", "unknown-id", "unknown-id",
            json.dumps({
                "Id": "unknown-id", "Created": "now", "Name": "foreign-postgres",
                "Image": "image", "Labels": {
                    "com.docker.compose.project": SUPPORT["PROJECT"],
                    "com.docker.compose.service": "postgres",
                    "com.docker.compose.oneoff": "False",
                    "aegis.test.resource-token": "foreign-token",
                },
            }, sort_keys=True, separators=(",", ":")),
        )
        record_globals = SUPPORT["resources_record"].__globals__
        monkeypatch.setitem(
            record_globals, "capture_project_inventory",
            lambda project: ProjectInventory(project, (resource,)),
        )

        with pytest.raises(RuntimeError, match="unexpected"):
            SUPPORT["resources_record"](path, "up")
        assert SUPPORT["_stored_resources"](SUPPORT["_load_ledger"](path)).resources == ()
    finally:
        SUPPORT["cleanup"](path)


def test_e2e_reads_host_credentials_before_subordinate_ownership_preparation() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/test-e2e.sh").read_text()

    # Locate the commands themselves, never the public phase-name allowlist.
    preparation = script.index("e2e_support.py prepare-runtime ")
    for variable in ("E2E_ALICE_PASSWORD", "E2E_BOB_PASSWORD", "E2E_ADMIN_PASSWORD"):
        assert script.index(f"read -r {variable}") < preparation
    assert script.index("e2e_support.py prepare-sources ") < script.index(
        "aegisctl mounts preflight "
    )


class _ComposeLoader(yaml.SafeLoader):
    """Resource-free Compose YAML reader: merge tags only change override semantics."""


def _compose_tag(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    assert isinstance(node, yaml.ScalarNode)
    return loader.construct_scalar(node)


for _tag in ("!override", "!reset"):
    _ComposeLoader.add_constructor(_tag, _compose_tag)

E2E_COMPOSE_FILES = ("compose.yaml", "compose.test.yaml", "compose.podman.yaml")
TOKEN_LABEL = "aegis.test.resource-token"
TOKEN_EXPRESSION = "${AEGIS_TEST_RESOURCE_TOKEN:?AEGIS_TEST_RESOURCE_TOKEN is required}"


def _compose_file(name: str) -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[2]
    loaded = yaml.load((repository / name).read_text(encoding="utf-8"), _ComposeLoader)
    assert isinstance(loaded, dict)
    return loaded


def _service_networks(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks")
    if networks is None:
        return set() if "network_mode" in service else {"default"}
    return set(networks)


def _service_named_volumes(service: dict[str, Any]) -> set[str]:
    named = set()
    for mount in service.get("volumes", []):
        if isinstance(mount, str):
            source, separator, _target = mount.partition(":")
            if separator and not source.startswith(("/", ".", "~", "$")):
                named.add(source)
        elif mount.get("type") == "volume" and mount.get("source"):
            named.add(mount["source"])
    return named


def _e2e_project_resources() -> tuple[set[str], set[str], set[str]]:
    """Services, networks and volumes the canonical E2E file set can create.

    Every enabled (profile-free) service's networks and named volumes, plus any
    declared top-level resource that no service uses: rules are at-most-once
    allowances, so admitting an unused declaration is safe whether or not the
    Compose release prunes it. Resources used only by profile-gated services are
    never created by the E2E ``up`` and must not be admitted.
    """
    files = [_compose_file(name) for name in E2E_COMPOSE_FILES]
    canonical = files[0]["services"]
    enabled = {name for name, service in canonical.items() if not service.get("profiles")}
    used: dict[bool, dict[str, set[str]]] = {
        state: {"networks": set(), "volumes": set()} for state in (True, False)
    }
    declared: dict[str, set[str]] = {"networks": set(), "volumes": set()}
    for document in files:
        for kind in declared:
            declared[kind] |= set(document.get(kind) or {})
        for name, service in (document.get("services") or {}).items():
            assert name in canonical, "an E2E overlay introduced an unreviewed service"
            state = name in enabled
            if document is files[0] or "networks" in service:
                used[state]["networks"] |= _service_networks(service)
            used[state]["volumes"] |= _service_named_volumes(service)
    expected = {
        kind: (declared[kind] | used[True][kind]) - (used[False][kind] - used[True][kind])
        for kind in declared
    }
    return enabled, expected["networks"], expected["volumes"]


def _rule_names(rules: tuple[Any, ...], kind: str, label: str) -> list[str]:
    return [
        dict(rule.required_labels)[label] for rule in rules if rule.kind == kind
    ]


def test_e2e_up_admission_exactly_covers_rendered_project_resources() -> None:
    services, networks, volumes = _e2e_project_resources()
    assert "tls-hop" in networks  # gateway's private TLS hop (compose.yaml).
    assert not {"caddy-data", "caddy-local-data"} & volumes  # TLS profiles only.
    token = "aegis-phase1-e2e.abcdefgh"
    rules = SUPPORT["_resource_rules"]("up", token)
    assert sorted(_rule_names(rules, "container", "com.docker.compose.service")) == sorted(
        services
    )
    assert sorted(_rule_names(rules, "network", "com.docker.compose.network")) == sorted(
        networks
    )
    assert sorted(_rule_names(rules, "volume", "com.docker.compose.volume")) == sorted(volumes)
    assert all((TOKEN_LABEL, token) in rule.required_labels for rule in rules)
    assert {rule.kind for rule in rules} == {"container", "network", "volume"}

    test_overlay = _compose_file("compose.test.yaml")
    for kind, expected in (("services", services), ("networks", networks),
                           ("volumes", volumes)):
        labelled = {
            name for name, entry in (test_overlay.get(kind) or {}).items()
            if (entry or {}).get("labels", {}).get(TOKEN_LABEL) == TOKEN_EXPRESSION
        }
        assert labelled == expected, f"compose.test.yaml {kind} provenance labels drifted"

    def labelled_resource(kind: str, name: str, key: str, extra: dict[str, str]) -> Any:
        labels = {
            "com.docker.compose.project": SUPPORT["PROJECT"], key: name, **extra,
        }
        section = {"container": "services", "network": "networks", "volume": "volumes"}[kind]
        entry = (test_overlay.get(section) or {}).get(name) or {}
        if entry.get("labels", {}).get(TOKEN_LABEL) == TOKEN_EXPRESSION:
            labels[TOKEN_LABEL] = token  # Compose applies the overlay's provenance label.
        identity = f"{kind}-{name}"
        return ProjectResource(kind, identity, identity, json.dumps(
            {"Id": identity, "Name": identity, "Labels": labels},
            sort_keys=True, separators=(",", ":"),
        ))

    created = (
        *(labelled_resource("container", name, "com.docker.compose.service",
                            {"com.docker.compose.oneoff": "False"}) for name in services),
        *(labelled_resource("network", name, "com.docker.compose.network", {})
          for name in networks),
        *(labelled_resource("volume", name, "com.docker.compose.volume", {})
          for name in volumes),
    )
    empty = ProjectInventory(SUPPORT["PROJECT"], ())
    observed = ProjectInventory(SUPPORT["PROJECT"], tuple(sorted(created)))
    assert admit_project_transition(empty, observed, rules) == observed

    # Admission stays exact: an unknown network with valid provenance is refused.
    foreign = ProjectResource("network", "foreign", "foreign", json.dumps(
        {"Id": "foreign", "Name": "foreign", "Labels": {
            "com.docker.compose.project": SUPPORT["PROJECT"],
            "com.docker.compose.network": "foreign", TOKEN_LABEL: token,
        }}, sort_keys=True, separators=(",", ":"),
    ))
    with pytest.raises(ProjectResourceError, match="unexpected"):
        admit_project_transition(
            empty, ProjectInventory(SUPPORT["PROJECT"], (*observed.resources, foreign)), rules,
        )


def test_e2e_refusal_is_one_bounded_public_annotation_only_under_github_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    path = private_e2e_directory()
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        record_globals = SUPPORT["main"].__globals__

        def refuse(project: str) -> ProjectInventory:
            raise ProjectResourceError("unexpected project resource transition")

        monkeypatch.setitem(record_globals, "capture_project_inventory", refuse)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with pytest.raises(ProjectResourceError):
            SUPPORT["main"](["resources-record", str(path), "up"])
        assert capsys.readouterr().out == ""

        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with pytest.raises(ProjectResourceError):
            SUPPORT["main"](["resources-record", str(path), "up"])
        assert capsys.readouterr().out.splitlines() == [
            "::error title=e2e-support resources-record::"
            "ProjectResourceError: unexpected project resource transition",
        ]
    finally:
        SUPPORT["cleanup"](path)


@pytest.mark.parametrize("error", (
    ValueError("invalid literal /tmp/aegis-phase1-e2e.private-sentinel"),
    ValueError("line one\nprivate-sentinel=%s"),
    ValueError("x" * 201),
    ValueError("tmp/aegis-phase1-e2e.private-sentinel"),
    ValueError("private-sentinel"),
    ProjectResourceError("private-sentinel project resource query failed"),
    RuntimeError("unexpected project resource transition"),
    KeyError("private-sentinel"),
    OSError(2, "No such file", "/srv/aegis/roots/private-sentinel"),
))
def test_e2e_refusal_annotation_omits_unfixed_or_path_details(error: Exception) -> None:
    annotation = SUPPORT["refusal_annotation"]("prepare", error)
    assert annotation == (
        f"::error title=e2e-support prepare::{type(error).__name__} (details omitted)"
    )
    assert "\n" not in annotation and "private-sentinel" not in annotation
    unknown = SUPPORT["refusal_annotation"]("x,y:%\n", ValueError("unknown E2E support action"))
    assert unknown == (
        "::error title=e2e-support unknown-action::ValueError: unknown E2E support action"
    )
    assert SUPPORT["_workflow_escape"]("a%b\r\nc:d,e", property_value=True) == (
        "a%25b%0D%0Ac%3Ad%2Ce"
    )


def test_e2e_public_refusals_are_exactly_the_constant_raised_messages() -> None:
    import ast

    repository = Path(__file__).resolve().parents[2]
    raised: dict[str, set[str]] = {"ValueError": set(), "ProjectResourceError": set()}
    for name in ("scripts/e2e_support.py", "backend/aegisctl/container_resources.py"):
        for node in ast.walk(ast.parse((repository / name).read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name) and node.exc.func.id in raised
            ):
                argument = node.exc.args[0]
                assert isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                raised[node.exc.func.id].add(argument.value)
    public = SUPPORT["_PUBLIC_REFUSALS"]
    assert {kind.__name__: set(messages) for kind, messages in public.items()} == raised
