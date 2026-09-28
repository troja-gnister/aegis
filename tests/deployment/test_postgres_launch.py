"""Resource-free regressions for canonical PostgreSQL preparation admission."""
from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from aegisctl import container_resources

from tests.deployment import test_container_boundaries as boundary
from tests.deployment.test_postgres_tmpfs import HELPER, initial_state, mutations, run_preparation
from tests.support.fake_container_engine import select_fake_engine

IMAGE = "sha256:" + "a" * 64
# The bootstrap program sources the fixed image path; only this maps it to the
# tracked helper. Kernel and utility boundaries remain the strict shell fakes.
IMAGE_HELPER = (
    "source() { [[ $1 == /usr/local/libexec/aegis-postgres-private-tmpfs.sh ]] || exit 98; "
    'builtin source "' + str(HELPER) + '"; }; '
)


def process_arguments(rendered: list[str]) -> list[str]:
    """Decode a validated Compose list exactly once, as the created process receives it."""
    return [value.replace("$$", "$") for value in rendered]


class PostgresEngine:
    """Strict CLI double; the real project admission/cleanup code remains active."""

    def __init__(self, prefix: list[str]) -> None:
        self.prefix = prefix
        self.project = ""
        self.containers: dict[str, dict[str, Any]] = {}
        self.volumes: dict[str, dict[str, Any]] = {}
        self.events: list[tuple[str, str]] = []
        self.stage = 0
        self.fault_stage = 1
        self.fault = ""
        self.create_failure = ""
        self.start_failure = ""
        self.bootstrap_exit = "0"
        self.bootstrap_logs: str | None = None
        self.inspected: dict[str, str] = {}
        # Parsed once: every modeled Compose call now renders to check interpolation.
        self.base = yaml.safe_load((boundary.REPOSITORY / "compose.yaml").read_text())

    def config(self, override: Path) -> dict[str, Any]:
        config = copy.deepcopy(self.base)
        extra = json.loads(override.read_text())
        config["services"]["postgres"].update(extra.get("services", {}).get("postgres", {}))
        config["secrets"].update(extra["secrets"])
        return config

    def interpolation_fails(self, override: Path) -> bool:
        """Real interpolation fails or substitutes a host value at any unpaired dollar."""
        service = self.config(override)["services"]["postgres"]
        return any(
            "$" in value.replace("$$", "")
            for key in ("entrypoint", "command") for value in service[key]
        )

    def create(self, override: Path) -> str:
        self.stage += 1
        identity = f"{self.stage:064x}"
        config = self.config(override)
        service = config["services"]["postgres"]
        declarations = dict(value.split(":", 1) for value in service["tmpfs"])
        mounts = [
            {"Type": "bind", "Source": config["secrets"][item["source"]]["file"],
             "Destination": item["target"], "RW": False}
            for item in service["secrets"]
        ]
        if self.prefix[0] == "podman":
            mounts += [{"Type": "tmpfs", "Destination": target} for target in declarations]
        info = {
            "Id": identity, "Created": f"created-{self.stage}",
            "Name": f"/{self.project}-postgres-1", "Image": IMAGE,
            "Config": {"User": service["user"],
                       "Entrypoint": process_arguments(service["entrypoint"]),
                       "Cmd": process_arguments(service["command"]), "Labels": {
                           "com.docker.compose.project": self.project,
                           "com.docker.compose.service": "postgres",
                           "com.docker.compose.oneoff": "False",
                       }},
            "HostConfig": {"Tmpfs": declarations, "ReadonlyRootfs": True, "Privileged": False},
            "Mounts": mounts,
        }
        if self.stage == self.fault_stage:
            if self.fault == "missing":
                del declarations["/run/secrets"]
            elif self.fault in {"bind", "volume"}:
                info["Mounts"] = [m for m in mounts if m["Destination"] != "/run/secrets"]
                info["Mounts"].append({"Type": self.fault, "Destination": "/run/secrets"})
            elif self.fault == "duplicate":
                mounts.append(copy.deepcopy(mounts[0]))
            elif self.fault == "alias":
                # An ambiguous duplicate: the canonical target plus its /var/run/
                # alias, both declared. Refused before start regardless of engine.
                declarations["/var/run/postgresql"] = declarations["/run/postgresql"]
            elif self.fault == "socket-alias":
                # The pre-fix declaration: only the /var/run/ alias, no duplicate.
                # Ambiguity-free before start; on the Podman double, a later
                # inspection (after start) loses it entirely (see O3/O4). Pops
                # whichever of the two keys is currently declared, so this does
                # not depend on compose.yaml's own (fixed or unfixed) state.
                present = (
                    declarations.pop("/run/postgresql", None)
                    or declarations.pop("/var/run/postgresql")
                )
                declarations["/var/run/postgresql"] = present
            elif self.fault == "no-mounts":
                del info["Mounts"]
            elif self.fault == "missing-source":
                mounts.pop(0)
            elif self.fault == "writable-source":
                mounts[0]["RW"] = True
            elif self.fault == "wrong-source":
                mounts[0]["Source"] = "/unowned/substitution"
            elif self.fault == "wrong-image":
                info["Image"] = "sha256:" + "b" * 64
            elif self.fault == "wrong-service":
                info["Config"]["Labels"]["com.docker.compose.service"] = "unrelated"
            elif self.fault == "wrong-command":
                info["Config"]["Cmd"] = ["other"]
            elif self.fault == "undecoded-entrypoint":
                # An engine that kept the Compose escapes in the process arguments.
                info["Config"]["Entrypoint"] = list(service["entrypoint"])
            elif self.fault == "changed-entrypoint":
                # A provider that substituted one Bash expression from the host.
                original = info["Config"]["Entrypoint"]
                changed = [value.replace("${target-}", "") for value in original]
                assert changed != original
                info["Config"]["Entrypoint"] = changed
            elif self.fault == "extended-entrypoint":
                *head, program = info["Config"]["Entrypoint"]
                info["Config"]["Entrypoint"] = [*head, program + "\nenv"]
        self.containers[identity] = info
        if isinstance(service["volumes"][0], str):
            name = f"{self.project}_postgres-data"
            self.volumes.setdefault(name, {
                "Name": name, "CreatedAt": "original-volume", "Driver": "local",
                "Mountpoint": "/synthetic/postgres-data", "Options": {}, "Scope": "local",
                "Labels": {"com.docker.compose.project": self.project,
                           "com.docker.compose.volume": "postgres-data"},
            })
        if self.stage == self.fault_stage:
            if self.fault == "no-container":
                self.containers.clear()
            elif self.fault == "unknown-create":
                self.add_unknown_container()
        return identity

    def add_unknown_container(self) -> None:
        unknown = copy.deepcopy(next(iter(self.containers.values())))
        unknown["Id"] = "f" * 64
        unknown["Name"] = "unrelated"
        unknown["Config"]["Labels"]["com.docker.compose.service"] = "unrelated"
        self.containers[unknown["Id"]] = unknown

    def __call__(
        self, command: list[str], *, inventory: bool = False, **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        assert command[:len(self.prefix)] == self.prefix
        args = command[len(self.prefix):]
        output = ""
        code = 0
        if args[0] == "compose":
            self.project = args[args.index("--project-name") + 1]
            override = Path(args[args.index("-f", args.index("-f") + 1) + 1])
            action, *options = args[args.index(str(override)) + 1:]
            self.events.append((action, str(self.stage)))
            if self.interpolation_fails(override):
                code = 1  # Compose fails while loading the project; nothing is created.
            elif action == "config":
                output = json.dumps(self.config(override))
            elif action == "build":
                assert options == ["postgres"]
            elif action in {"create", "up"}:
                if action == "create":
                    assert options == ["--no-build", "--pull", "never", "postgres"]
                identity = self.create(override)
                self.events.append(("created", identity))
                if action == "up":
                    self.events.append(("helper", identity))
                if self.create_failure == "interrupt":
                    raise KeyboardInterrupt
                if self.create_failure == "timeout":
                    raise subprocess.TimeoutExpired(command, 1)
                code = 125 if self.create_failure == "nonzero" else 0
            elif action == "ps":
                output = next(iter(self.containers), "")
            else:
                assert action == "exec" and options[0] == "-T"
        elif args[:2] == ["image", "inspect"]:
            assert args[2:] == ["aegis-postgres"]
            self.events.append(("image", IMAGE))
            output = json.dumps([{"Id": IMAGE}])
        elif len(args) >= 2 and args[1] == "ls":
            kind = args[0]
            project = self.project or args[-1].split("=", 2)[-1]
            assert args == [kind, "ls", *(["--all"] if kind == "container" else []),
                            "--quiet", "--filter", f"label=com.docker.compose.project={project}"]
            output = "\n".join(self.containers if kind == "container" else
                               self.volumes if kind == "volume" else ())
        elif args[:2] == ["container", "inspect"]:
            identity = args[2]
            if identity not in self.containers:
                code = 1
            else:
                info = self.containers[identity]
                if inventory and self.fault == "inventory-failure":
                    code = 125
                elif not inventory:
                    self.events.append(("inspect", identity))
                    if self.stage == self.fault_stage:
                        if self.fault == "inspection-failure":
                            code = 125
                        elif self.fault == "identity-drift":
                            info["Created"] = "replacement"
                        elif self.fault == "ownership-drift":
                            info["Config"]["Labels"]["com.docker.compose.project"] = "other"
                        elif self.fault == "image-drift":
                            info["Image"] = "sha256:" + "b" * 64
                        elif self.fault == "unknown-inspect":
                            self.add_unknown_container()
                payload = info
                if self.prefix[0] == "podman" and ("start", identity) in self.events:
                    # Measured rootless Podman behavior (O3/O4): the image's
                    # /var/run -> ../run symlink is resolved once the container has
                    # started, so a tmpfs declared under the /var/run/ alias no
                    # longer appears in HostConfig.Tmpfs on any later inspection.
                    payload = copy.deepcopy(info)
                    declarations = payload["HostConfig"]["Tmpfs"]
                    payload["HostConfig"]["Tmpfs"] = {
                        target: options for target, options in declarations.items()
                        if not target.startswith("/var/run/")
                    }
                output = json.dumps([payload])
                if not inventory:
                    # Each identity's first engine inspection is its pre-start proof.
                    self.inspected.setdefault(identity, output)
        elif args[:2] == ["volume", "inspect"]:
            if args[2] == "aegis_postgres-data":
                code = 1
            else:
                output = json.dumps([self.volumes[args[2]]])
        elif args[0] == "start":
            assert len(args) == 2 and args[1] in self.containers
            self.events.extend([("start", args[1]), ("helper", args[1])])
            if self.fault == "unknown-start":
                self.add_unknown_container()
            if self.start_failure == "interrupt":
                raise KeyboardInterrupt
            if self.start_failure == "timeout":
                raise subprocess.TimeoutExpired(command, 1)
            code = 125 if self.start_failure == "nonzero" else 0
        elif args[0] == "wait":
            assert args[1] in self.containers
            self.events.append(("wait", args[1]))
            output = self.bootstrap_exit
        elif args[0] == "logs":
            identity = args[-1]
            assert identity in self.containers
            info = self.containers[identity]
            if info["Config"]["Cmd"] == []:
                # The bootstrap must be bounded before the exit-code assertion.
                assert args == ["logs", "--tail", "32", identity]
            else:
                assert args == ["logs", identity]
            self.events.append(("logs", identity))
            output = ("0:0:700:/run/aegis-source-secrets\n70:70:700:/run/secrets\n"
                      "70:70:775:/run/postgresql\n70:70:1777:/tmp\n"
                      if info["Config"]["Cmd"] == [] else "database secret staging refused")
            if info["Config"]["Cmd"] == [] and self.bootstrap_logs is not None:
                output = self.bootstrap_logs
        elif args[0] == "inspect":
            assert args[1] in self.containers
            assert args[2:] == ["--format", "{{if .State.Health}}{{.State.Health.Status}}{{end}}"]
            output = "healthy"
        elif args[:2] == ["rm", "--force"]:
            self.events.append(("remove", args[2]))
            del self.containers[args[2]]
        elif args[:2] == ["volume", "rm"]:
            self.events.append(("remove-volume", args[2]))
            del self.volumes[args[2]]
        else:
            raise AssertionError(f"unexpected fake engine command: {args}")
        if code and kwargs.get("check"):
            raise subprocess.CalledProcessError(code, command, output=output)
        return subprocess.CompletedProcess(command, code, output, "")


@pytest.fixture(params=["docker", "podman"])
def engine(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> PostgresEngine:
    prefix = select_fake_engine(request.param, tmp_path, monkeypatch, checked_mask_policy=True)
    fake = PostgresEngine(prefix)
    monkeypatch.setattr(boundary, "CONTAINER_COMMAND", prefix)
    monkeypatch.setattr(boundary, "PROJECT_INVENTORIES", {})
    monkeypatch.setattr(boundary, "run_container", fake)
    monkeypatch.setattr(boundary, "prepare_owned_test_inventory", lambda inventory: None)
    monkeypatch.setattr(boundary.time, "sleep", lambda duration: None)
    monkeypatch.setattr(
        container_resources, "_run", lambda arguments, environment: fake(arguments, inventory=True),
    )
    return fake


@pytest.mark.parametrize(("journey", "stage"), [
    ("fresh", 1), ("fresh", 2), ("reconcile", 1), ("reconcile", 2), ("reconcile", 3),
], ids=["bootstrap", "fresh", "accepted-base", "empty-secret", "rotated"])
@pytest.mark.parametrize("fault", ["bind", "missing", "inspection-failure"])
def test_every_canonical_launch_refuses_substitution_before_preparation(
    engine: PostgresEngine, tmp_path: Path, journey: str, stage: int, fault: str,
) -> None:
    engine.fault = fault
    engine.fault_stage = stage
    source = tmp_path / "synthetic-sources"
    source.mkdir(mode=0o700)
    launch = (boundary.test_live_postgres_stages_secrets_and_drops_to_uid_70 if journey == "fresh"
              else boundary.test_postgres_reconciles_populated_accepted_base_and_rotated_secrets)
    bad_id = f"{stage:064x}"
    try:
        if fault == "inspection-failure":
            with pytest.raises(subprocess.CalledProcessError):
                launch(source)
        else:
            message = "was substituted" if fault == "bind" else "declaration missing"
            with pytest.raises(AssertionError, match=message):
                launch(source)
    finally:
        assert ("helper", bad_id) not in engine.events, engine.events
    assert ("inspect", bad_id) in engine.events
    assert ("remove", bad_id) in engine.events
    assert engine.containers == {} and engine.volumes == {}
    assert not any(event in {"ps", "up"} for event, _ in engine.events)


def launch_inputs(tmp_path: Path) -> Path:
    override = tmp_path / "canonical.json"
    override.write_text(json.dumps({
        "services": {}, "secrets": {
            name: {"file": str(tmp_path / name)} for name in boundary.POSTGRES_SECRET_SOURCES
        },
    }))
    return override


@pytest.mark.parametrize("fault", [
    "missing", "bind", "volume", "duplicate", "alias", "no-mounts", "missing-source",
    "writable-source", "wrong-source", "wrong-image", "wrong-command", "inspection-failure",
])
def test_effective_provenance_is_required_before_start(
    engine: PostgresEngine, tmp_path: Path, fault: str,
) -> None:
    engine.fault = fault
    override = launch_inputs(tmp_path)
    image = boundary.build_postgres_image("provenance", override)
    with pytest.raises((AssertionError, subprocess.CalledProcessError)):
        boundary.start_postgres_with_private_tmpfs("provenance", override, image)
    assert not any(event == "start" for event, _ in engine.events)
    assert len(boundary.PROJECT_INVENTORIES["provenance"].resources) == 2
    boundary.docker_compose("provenance", override, "down", "--volumes")
    assert engine.containers == {} and engine.volumes == {}


def test_canonical_socket_declaration_survives_podman_start_alias_resolution(
    engine: PostgresEngine, tmp_path: Path,
) -> None:
    """Root brief R4: the socket tmpfs must be declared at its effective path.

    Measured rootless Podman resolves the image's /var/run -> ../run symlink at
    start; a tmpfs declaration made under the /var/run/ alias is then omitted
    from any later HostConfig.Tmpfs inspection (O3/O4). A declaration already
    made at the canonical /run/postgresql path is unaffected by that resolution
    and must keep passing provenance both before start and afterward, on every
    engine, including the Podman double.
    """
    override = launch_inputs(tmp_path)
    image = boundary.build_postgres_image("socket-canonical", override)
    container_id = boundary.start_postgres_with_private_tmpfs(
        "socket-canonical", override, image,
    )
    boundary.inspect_postgres_private_tmpfs(container_id)
    boundary.docker_compose("socket-canonical", override, "down", "--volumes")
    assert engine.containers == {} and engine.volumes == {}


@pytest.mark.parametrize("engine", ["podman"], indirect=True)
def test_alias_declared_socket_tmpfs_fails_the_podman_post_start_check(
    engine: PostgresEngine, tmp_path: Path,
) -> None:
    """Pins the reason for the R4 change.

    Had the socket tmpfs stayed declared under the /var/run/ alias, it would
    pass the pre-start check (no ambiguity: there is only one key) but fail a
    later provenance check on the Podman double, exactly reproducing the root
    brief's measured evidence (O3/O4). Docker does not resolve or drop the
    alias, so this is Podman-specific: the fixture's ["docker", "podman"]
    parametrization is overridden above to run only the Podman case, rather
    than skipping the Docker one at runtime. The double applies the omission
    starting from the "start" event; real Podman was measured only after the
    container exited (O3/O4), which is equivalent here because nothing in the
    intervening fake "wait"/"logs" steps ever restores a dropped declaration.
    """
    engine.fault = "socket-alias"
    override = launch_inputs(tmp_path)
    image = boundary.build_postgres_image("socket-alias", override)
    container_id = boundary.start_postgres_with_private_tmpfs(
        "socket-alias", override, image,
    )
    with pytest.raises(AssertionError, match="declaration missing"):
        boundary.inspect_postgres_private_tmpfs(container_id)
    boundary.docker_compose("socket-alias", override, "down", "--volumes")
    assert engine.containers == {} and engine.volumes == {}


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "interrupt"])
def test_failed_create_records_partial_resources_for_exact_cleanup(
    engine: PostgresEngine, tmp_path: Path, failure: str,
) -> None:
    engine.create_failure = failure
    override = launch_inputs(tmp_path)
    image = boundary.build_postgres_image("partial", override)
    with pytest.raises((AssertionError, subprocess.TimeoutExpired, KeyboardInterrupt)):
        boundary.start_postgres_with_private_tmpfs("partial", override, image)
    recorded = boundary.PROJECT_INVENTORIES["partial"]
    assert {
        resource.immutable_id for resource in recorded.resources if resource.kind == "container"
    } == {f"{1:064x}"}
    assert not any(event == "start" for event, _ in engine.events)
    boundary.docker_compose("partial", override, "down", "--volumes")
    assert engine.containers == {} and engine.volumes == {}


@pytest.mark.parametrize("fault", [
    "identity-drift", "ownership-drift", "image-drift", "wrong-service",
    "unknown-create", "unknown-inspect", "inventory-failure",
])
def test_unknown_or_changed_ownership_is_not_adopted_or_cleaned(
    engine: PostgresEngine, tmp_path: Path, fault: str,
) -> None:
    engine.fault = fault
    override = launch_inputs(tmp_path)
    image = boundary.build_postgres_image("ownership", override)
    with pytest.raises((AssertionError, container_resources.ProjectResourceError)):
        boundary.start_postgres_with_private_tmpfs("ownership", override, image)
    recorded = boundary.PROJECT_INVENTORIES["ownership"]
    with pytest.raises(container_resources.ProjectResourceError):
        boundary.docker_compose("ownership", override, "down", "--volumes")
    assert boundary.PROJECT_INVENTORIES["ownership"] == recorded
    assert not any(event in {"start", "remove", "remove-volume"} for event, _ in engine.events)
    assert engine.containers and engine.volumes


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "interrupt"])
def test_failed_create_does_not_admit_unexpected_resources(
    engine: PostgresEngine, tmp_path: Path, failure: str,
) -> None:
    engine.fault = "unknown-create"
    engine.create_failure = failure
    override = launch_inputs(tmp_path)
    image = boundary.build_postgres_image("unknown-partial", override)
    before = boundary.PROJECT_INVENTORIES["unknown-partial"]
    with pytest.raises(container_resources.ProjectResourceError, match="unexpected"):
        boundary.start_postgres_with_private_tmpfs("unknown-partial", override, image)
    assert boundary.PROJECT_INVENTORIES["unknown-partial"] == before
    with pytest.raises(container_resources.ProjectResourceError):
        boundary.docker_compose("unknown-partial", override, "down", "--volumes")
    assert not any(event in {"start", "remove", "remove-volume"} for event, _ in engine.events)
    assert len(engine.containers) == 2


def test_missing_created_container_is_never_adopted_by_name(
    engine: PostgresEngine, tmp_path: Path,
) -> None:
    engine.fault = "no-container"
    override = launch_inputs(tmp_path)
    image = boundary.build_postgres_image("missing", override)
    with pytest.raises(AssertionError, match="identity is missing"):
        boundary.start_postgres_with_private_tmpfs("missing", override, image)
    assert not any(event in {"ps", "start"} for event, _ in engine.events)
    boundary.docker_compose("missing", override, "down", "--volumes")
    assert not engine.containers and not engine.volumes


def test_start_cannot_admit_unrelated_resources(
    engine: PostgresEngine, tmp_path: Path,
) -> None:
    engine.fault = "unknown-start"
    override = launch_inputs(tmp_path)
    image = boundary.build_postgres_image("start-drift", override)
    with pytest.raises(container_resources.ProjectResourceError, match="unknown resources"):
        boundary.start_postgres_with_private_tmpfs("start-drift", override, image)
    recorded = boundary.PROJECT_INVENTORIES["start-drift"]
    assert {
        resource.immutable_id for resource in recorded.resources if resource.kind == "container"
    } == {f"{1:064x}"}
    with pytest.raises(container_resources.ProjectResourceError):
        boundary.docker_compose("start-drift", override, "down", "--volumes")
    assert boundary.PROJECT_INVENTORIES["start-drift"] == recorded
    assert not any(event in {"remove", "remove-volume"} for event, _ in engine.events)
    assert len(engine.containers) == 2


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "interrupt"])
def test_failed_start_retains_only_previously_admitted_resources(
    engine: PostgresEngine, tmp_path: Path, failure: str,
) -> None:
    engine.start_failure = failure
    override = launch_inputs(tmp_path)
    image = boundary.build_postgres_image("failed-start", override)
    with pytest.raises((
        subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyboardInterrupt,
    )):
        boundary.start_postgres_with_private_tmpfs("failed-start", override, image)
    assert len(boundary.PROJECT_INVENTORIES["failed-start"].resources) == 2
    boundary.docker_compose("failed-start", override, "down", "--volumes")
    assert not engine.containers and not engine.volumes


def test_created_identity_is_inspected_before_start_and_database_survives_recreation(
    engine: PostgresEngine, tmp_path: Path,
) -> None:
    override = launch_inputs(tmp_path)
    image = boundary.build_postgres_image("ordered", override)
    first = boundary.start_postgres_with_private_tmpfs("ordered", override, image)
    assert first == f"{1:064x}"
    assert [(event, value) for event, value in engine.events if event in {
        "image", "created", "inspect", "start", "helper", "up", "ps",
    }] == [("image", IMAGE), ("created", first), ("inspect", first), ("start", first),
           ("helper", first)]
    assert boundary.wait_for_postgres_health("ordered", first) == first
    volume = copy.deepcopy(engine.volumes)
    boundary.docker_compose("ordered", override, "down")
    assert not engine.containers and engine.volumes == volume
    second = boundary.start_postgres_with_private_tmpfs("ordered", override, image)
    assert second == f"{2:064x}" and first != second
    assert engine.volumes == volume
    boundary.docker_compose("ordered", override, "down", "--volumes")
    assert engine.containers == {} and engine.volumes == {}


@pytest.mark.parametrize("subject", ["application", "postgres"])
@pytest.mark.parametrize("drift", [
    "none", "missing-nnp", "missing-mask", "extra-mask", "duplicate-nnp", "duplicate-mask",
])
def test_static_security_requires_exact_engine_options(
    engine: PostgresEngine, monkeypatch: pytest.MonkeyPatch, subject: str, drift: str,
) -> None:
    options = ["no-new-privileges:true"]
    if engine.prefix[0] == "podman":
        options.append("unmask=/sys/devices/virtual/powercap")
    if drift == "missing-nnp":
        options.remove("no-new-privileges:true")
    elif drift == "missing-mask":
        options = [value for value in options if not value.startswith("unmask=")]
    elif drift == "extra-mask":
        options.append("unmask=ALL")
    elif drift == "duplicate-nnp":
        options.append("no-new-privileges:true")
    elif drift == "duplicate-mask":
        options.append("unmask=/sys/devices/virtual/powercap")
    if subject == "postgres":
        config = yaml.safe_load((boundary.REPOSITORY / "compose.yaml").read_text())
        config["services"]["postgres"]["build"]["context"] = str(boundary.REPOSITORY)
        config["services"]["postgres"]["security_opt"] = options
        assertion = boundary.test_postgres_stages_fixed_source_secrets_into_uid_70_private_tmpfs
    else:
        config = {
            "services": {name: {
                "read_only": True, "cap_drop": ["ALL"], "security_opt": options,
                "user": "10001:10001", "tmpfs": ["/tmp:size=64m,noexec,nosuid,nodev"],
                "volumes": [], "networks": ["backend"],
            } for name in boundary.APPLICATION_SERVICES},
            "networks": {"backend": {"internal": True}},
        }
        assertion = boundary.test_application_services_have_fail_closed_container_isolation
    monkeypatch.setattr(boundary, "rendered_compose", lambda: config)
    valid = drift == "none" or (drift == "missing-mask" and engine.prefix[0] == "docker")
    if valid:
        assertion()
    else:
        with pytest.raises(AssertionError):
            assertion()


@pytest.mark.parametrize("oversized", [False, True])
def test_bootstrap_failure_collects_bounded_diagnostics_before_cleanup(
    engine: PostgresEngine, tmp_path: Path, oversized: bool,
) -> None:
    engine.bootstrap_exit = "1"
    diagnostic = "bootstrap-refusal frame=1 function=aegis_pg_require_node line=53"
    engine.bootstrap_logs = ("x" * 8193 if oversized else
                             "database secret staging refused\n" + diagnostic + "\n")
    source = tmp_path / "synthetic-sources"
    source.mkdir(mode=0o700)
    with pytest.raises(AssertionError) as caught:
        boundary.test_live_postgres_stages_secrets_and_drops_to_uid_70(source)
    identity = f"{1:064x}"
    assert engine.events.index(("wait", identity)) < engine.events.index(("logs", identity))
    assert engine.events.index(("logs", identity)) < engine.events.index(("remove", identity))
    if oversized:
        assert str(caught.value) == "bootstrap diagnostic logs exceeded 8192 bytes"
        assert "x" * 100 not in str(caught.value)
    else:
        assert diagnostic in str(caught.value)
    assert not engine.containers and not engine.volumes
    assert engine.stage == 1  # A failed probe never reaches the normal entrypoint.


def test_bootstrap_refusal_identifies_real_guard_without_sensitive_state(tmp_path: Path) -> None:
    state = initial_state()
    state["nodes"]["/tmp"]["mode"] = 0o777
    invocation = (boundary.POSTGRES_BOOTSTRAP_DIAGNOSTIC + "\n"
                  "private_canary='do-not-log-this-secret'; aegis_prepare_postgres_tmpfs")
    completed, observed = run_preparation(tmp_path, state, invocation=invocation)
    assert completed.returncode == 1 and completed.stdout == ""
    assert "database secret staging refused\n" in completed.stderr
    assert "bootstrap-refusal target=/tmp\n" in completed.stderr
    assert re.search(r"function=aegis_pg_require_node line=[1-9][0-9]*", completed.stderr)
    assert "do-not-log-this-secret" not in completed.stderr
    assert "mountinfo" not in completed.stderr and "private_canary" not in completed.stderr
    assert len(completed.stderr.encode()) < 1024 and mutations(observed) == []


def test_bootstrap_refusal_caps_stack_and_rejects_nonallowlisted_target(tmp_path: Path) -> None:
    invocation = boundary.POSTGRES_BOOTSTRAP_DIAGNOSTIC + "\n"
    invocation += "target='do-not-log-this-secret'; "
    for index in range(9):
        invocation += f"frame{index}() {{ frame{index + 1}; }}; "
    invocation += "frame9() { aegis_pg_refuse; }; frame0"
    completed, _ = run_preparation(tmp_path, initial_state(), invocation=invocation)
    assert completed.returncode == 1 and completed.stdout == ""
    assert completed.stderr.count("bootstrap-refusal frame=") == 6
    assert "bootstrap-refusal target=unavailable\n" in completed.stderr
    assert "do-not-log-this-secret" not in completed.stderr
    assert len(completed.stderr.encode()) < 1024


def test_bootstrap_unhandled_failure_reports_only_line_and_status(tmp_path: Path) -> None:
    invocation = (boundary.POSTGRES_BOOTSTRAP_DIAGNOSTIC + "\n"
                  "private_canary='do-not-log-this-secret'; false; printf UNREACHABLE")
    completed, _ = run_preparation(tmp_path, initial_state(), invocation=invocation)
    assert completed.returncode == 1 and completed.stdout == ""
    assert re.fullmatch(r"bootstrap-error line=[1-9][0-9]* status=1\n", completed.stderr)


def test_successful_bootstrap_diagnostic_is_silent_and_preserves_preparation(
    tmp_path: Path,
) -> None:
    completed, observed = run_preparation(
        tmp_path, initial_state(),
        invocation=boundary.POSTGRES_BOOTSTRAP_DIAGNOSTIC + "\naegis_prepare_postgres_tmpfs",
    )
    assert completed.returncode == 0 and completed.stdout == "READY\n" and completed.stderr == ""
    assert len(mutations(observed)) == 6


def test_bootstrap_starts_only_after_exact_literal_program_inspection(
    engine: PostgresEngine, tmp_path: Path,
) -> None:
    engine.fault, engine.fault_stage = "bind", 2  # Stop at the following normal launch.
    source = tmp_path / "synthetic-sources"
    source.mkdir(mode=0o700)
    with pytest.raises(AssertionError, match="was substituted"):
        boundary.test_live_postgres_stages_secrets_and_drops_to_uid_70(source)
    identity = f"{1:064x}"
    rendered = engine.config(source / "compose.bootstrap.json")["services"]["postgres"]
    assert rendered["entrypoint"] == boundary.POSTGRES_BOOTSTRAP_COMPOSE_ENTRYPOINT
    assert rendered["entrypoint"] != list(boundary.POSTGRES_BOOTSTRAP_ENTRYPOINT)
    inspected = json.loads(engine.inspected[identity])[0]["Config"]
    assert inspected["Entrypoint"] == list(boundary.POSTGRES_BOOTSTRAP_ENTRYPOINT)
    assert inspected["Cmd"] == []
    assert [event for event in engine.events if event[1] == identity] == [
        ("created", identity), ("inspect", identity), ("start", identity), ("helper", identity),
        ("wait", identity), ("logs", identity), ("inspect", identity), ("remove", identity),
    ]
    assert engine.containers == {} and engine.volumes == {}


@pytest.mark.parametrize("fault", [
    "undecoded-entrypoint", "changed-entrypoint", "extended-entrypoint",
])
def test_altered_bootstrap_entrypoint_is_refused_before_start(
    engine: PostgresEngine, tmp_path: Path, fault: str,
) -> None:
    engine.fault = fault
    source = tmp_path / "synthetic-sources"
    source.mkdir(mode=0o700)
    with pytest.raises(AssertionError, match="PostgreSQL entrypoint changed"):
        boundary.test_live_postgres_stages_secrets_and_drops_to_uid_70(source)
    identity = f"{1:064x}"
    assert not any(event in {"start", "helper"} for event, _ in engine.events), engine.events
    assert [event for event in engine.events if event[1] == identity] == [
        ("created", identity), ("inspect", identity), ("remove", identity),
    ]
    assert engine.containers == {} and engine.volumes == {}


def test_bootstrap_compose_entrypoint_must_be_encoded_exactly_once(
    engine: PostgresEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A deliberately wrong representation: every literal dollar encoded twice.
    twice = [value.replace("$", "$$$$") for value in boundary.POSTGRES_BOOTSTRAP_ENTRYPOINT]
    monkeypatch.setattr(boundary, "POSTGRES_BOOTSTRAP_COMPOSE_ENTRYPOINT", twice)
    source = tmp_path / "synthetic-sources"
    source.mkdir(mode=0o700)
    with pytest.raises(AssertionError, match="PostgreSQL Compose entrypoint changed"):
        boundary.test_live_postgres_stages_secrets_and_drops_to_uid_70(source)
    identity = f"{1:064x}"
    assert not any(event in {"start", "helper"} for event, _ in engine.events), engine.events
    assert [event for event in engine.events if event[1] == identity] == [
        ("created", identity), ("inspect", identity), ("remove", identity),
    ]
    assert engine.containers == {} and engine.volumes == {}


def test_unencoded_bootstrap_program_is_refused_before_creation(
    engine: PostgresEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The frozen D1 representation: the literal vector written without encoding.
    monkeypatch.setattr(
        boundary, "POSTGRES_BOOTSTRAP_COMPOSE_ENTRYPOINT",
        list(boundary.POSTGRES_BOOTSTRAP_ENTRYPOINT),
    )
    source = tmp_path / "synthetic-sources"
    source.mkdir(mode=0o700)
    with pytest.raises(AssertionError):
        boundary.test_live_postgres_stages_secrets_and_drops_to_uid_70(source)
    assert engine.events == [("config", "0"), ("build", "0"), ("image", IMAGE), ("config", "0")]
    created = boundary.docker_compose(
        engine.project, source / "compose.bootstrap.json",
        "create", "--no-build", "--pull", "never", "postgres",
    )
    assert created.returncode == 1
    assert engine.stage == 0 and engine.containers == {} and engine.volumes == {}


def test_bootstrap_err_handler_never_returns() -> None:
    # bash 5.2 prints "pop_var_context: head of shell_variables not a function
    # context" when an ERR-trap handler function executes `return`; errexit
    # already exits with the failing command's status, so the handler must not.
    match = re.search(
        r"^aegis_pg_bootstrap_error\(\) \{\n(.*?)^\}\n",
        boundary.POSTGRES_BOOTSTRAP_DIAGNOSTIC, re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    assert re.search(r"\breturn\b", match.group(1)) is None


@pytest.mark.parametrize("outcome", ["refusal", "unhandled"])
def test_inspected_bootstrap_program_reaches_bash_unchanged(
    engine: PostgresEngine, tmp_path: Path, outcome: str,
) -> None:
    engine.fault, engine.fault_stage = "bind", 2  # Stop at the following normal launch.
    source = tmp_path / "synthetic-sources"
    source.mkdir(mode=0o700)
    with pytest.raises(AssertionError, match="was substituted"):
        boundary.test_live_postgres_stages_secrets_and_drops_to_uid_70(source)
    # Execute the process argument recovered after the modeled Compose round trip.
    process = json.loads(engine.inspected[f"{1:064x}"])[0]["Config"]["Entrypoint"]
    assert process == list(boundary.POSTGRES_BOOTSTRAP_ENTRYPOINT)
    program = process[2]
    for expression in (
        "${target-}", "${#FUNCNAME[@]}", "$frame", "$?", "$LINENO",
        "${FUNCNAME[frame]}", "${BASH_LINENO[frame-1]}",
    ):
        assert expression in program
    state = initial_state()
    invocation = IMAGE_HELPER + program
    if outcome == "refusal":
        state["nodes"]["/tmp"]["mode"] = 0o777
    else:
        # Preparation succeeds; only the program's final metadata command fails.
        invocation = (
            'stat() { [[ $* != "-c %u:%g:%a:%n /run/aegis-source-secrets /run/secrets '
            '/run/postgresql /tmp" ]] || return 7; command stat "$@"; }; ' + invocation
        )
    completed, observed = run_preparation(tmp_path, state, invocation=invocation)
    assert completed.stdout == ""
    if outcome == "refusal":
        assert completed.returncode == 1 and mutations(observed) == []
        assert re.fullmatch(
            r"database secret staging refused\n"
            r"bootstrap-refusal target=/tmp\n"
            r"bootstrap-refusal frame=1 function=aegis_pg_require_node line=[1-9][0-9]*\n"
            r"bootstrap-refusal frame=2 function=aegis_prepare_postgres_tmpfs line=[1-9][0-9]*\n"
            r"bootstrap-error line=[1-9][0-9]* status=1\n",
            completed.stderr,
        )
    else:
        assert completed.returncode == 7 and len(mutations(observed)) == 6
        # The harness adds no newline before the program, so its line numbers hold.
        final_line = 1 + program.count("\n")
        assert completed.stderr == f"bootstrap-error line={final_line} status=7\n"
