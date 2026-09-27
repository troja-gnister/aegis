"""Run the actual shell validator with fake kernel/utility boundaries, never mounts.

The doubles model stat/mountinfo and record directory mutations. They cannot chown
host paths, open a database, or start a container. Parsing and preparation ordering
remain the production shell's responsibility.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
HELPER = REPOSITORY / "deploy/postgres/private-tmpfs.sh"
DESTINATIONS = ("/run/secrets", "/run/postgresql", "/tmp")
SOURCE = "/run/aegis-source-secrets"
SECRETS = (
    "postgres_superuser_password",
    "db_migrator_password",
    "db_web_password",
    "db_operations_password",
    "db_indexer_password",
    "db_media_password",
)


def initial_state() -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    for index, (path, mode) in enumerate(
        (
            ("/", 0o755),
            ("/var", 0o755),
            ("/run", 0o755),
            (SOURCE, 0o700),
            (DESTINATIONS[0], 0o700),
            (DESTINATIONS[1], 0o775),
            (DESTINATIONS[2], 0o1777),
        ),
        start=10,
    ):
        nodes[path] = dict(
            dev=index, ino=1, kind=stat.S_IFDIR, uid=0, gid=0, mode=mode, size=40, entries=[]
        )
    nodes["/var/run"] = dict(
        dev=11, ino=2, kind=stat.S_IFLNK, uid=0, gid=0, mode=0o777, size=6, target="../run"
    )
    lines = ["1 0 0:10 / / ro - overlay overlay ro"]
    for index, path in enumerate((SOURCE, *DESTINATIONS), start=100):
        size = "64k" if path in (SOURCE, DESTINATIONS[0]) else "16384k"
        lines.append(
            f"{index} 1 0:{nodes[path]['dev']} / {path} "
            f"rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs "
            f"rw,size={size},uid=1000,gid=1000"
        )
    for index, name in enumerate(SECRETS, start=200):
        path = f"{SOURCE}/{name}"
        nodes[path] = dict(dev=99, ino=index, kind=stat.S_IFREG, uid=0, gid=0, mode=0o600, size=24)
        nodes[SOURCE]["entries"].append(name)
        lines.append(f"{index} 100 0:99 /synthetic/{name} {path} ro - btrfs /dev/test rw")
    return dict(
        nodes=nodes,
        mountinfo="\n".join(lines) + "\n",
        uid=0,
        gid=0,
        calls=[],
        changes=[],
        fail=None,
    )


FAKE_UTILITY = r"""import json, os, signal, sys
from pathlib import Path
path = Path(os.environ["AEGIS_SHELL_TEST_STATE"])
state = json.loads(path.read_text())
command = Path(sys.argv[0]).name
args = sys.argv[1:]
state["calls"].append([command, *args])
occurrence = sum(row == [command, *args] for row in state["calls"])
for change in state["changes"]:
    if change["command"] == command and change.get("path", args[-1]) == args[-1] \
            and change.get("occurrence", 1) == occurrence:
        if "node" in change:
            state["nodes"][change.get("target", args[-1])].update(change["node"])
        if "mountinfo" in change:
            state["mountinfo"] = change["mountinfo"]
failure = state.get("fail") or {}
failed = failure.get("command") == command and failure.get("path", args[-1]) == args[-1] \
    and failure.get("occurrence", 1) == occurrence
output = ""
status = 0
if failed and failure.get("action") != "wrong-state":
    status = 19
elif command == "id":
    output = str(state["uid" if args == ["-u"] else "gid"]) + "\n"
elif command == "dd":
    assert args == ["if=/proc/self/mountinfo", "bs=1048577", "count=1", "iflag=fullblock"]
    output = state["mountinfo"][:1048577]
elif command == "stat":
    assert args[:2] == ["-c", "%d:%i:%f:%u:%g:%a:%s"]
    node = state["nodes"].get(args[-1])
    if node is None:
        status = 1
    else:
        output = "%d:%d:%x:%d:%d:%o:%d\n" % (node["dev"], node["ino"],
            node["kind"] | node["mode"], node["uid"], node["gid"], node["mode"], node["size"])
elif command == "readlink":
    assert args == ["/var/run"]
    output = state["nodes"]["/var/run"].get("target", "") + "\n"
elif command == "test":
    assert args[0] == "-r" and args[1].startswith("/run/aegis-source-secrets/")
    status = 0 if state["nodes"][args[1]].get("readable", True) else 1
elif command == "find":
    assert args[1:] in (["-mindepth", "1", "-maxdepth", "1", "-print"],
                       ["-mindepth", "1", "-maxdepth", "1", "-print", "-quit"])
    entries = state["nodes"][args[0]]["entries"]
    if args[-1] == "-quit":
        entries = entries[:1]
    output = "".join(args[0] + "/" + name + "\n" for name in entries)
elif command in ("chown", "chmod"):
    assert args[1] == "--" and len(args) == 3
    assert args[2] in ("/run/secrets", "/run/postgresql", "/tmp")
    if not failed:
        if command == "chown":
            assert args[0] == "70:70"
            state["nodes"][args[2]].update(uid=70, gid=70)
        else:
            state["nodes"][args[2]]["mode"] = int(args[0], 8)
    elif command == "chmod":
        state["nodes"][args[2]]["mode"] = 0o777
else:
    raise AssertionError((command, args))
path.write_text(json.dumps(state))
if failed and failure.get("action") == "interrupt":
    os.kill(os.getppid(), signal.SIGTERM)
sys.stdout.write(output)
sys.exit(status)
"""


def run_preparation(
    tmp_path: Path,
    state: dict[str, Any],
    *,
    helper: Path = HELPER,
    invocation: str = "aegis_prepare_postgres_tmpfs",
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("stat", "id", "dd", "readlink", "find", "chown", "chmod", "test"):
        executable = bin_dir / name
        executable.write_text(f"#!{sys.executable} -S\n" + FAKE_UTILITY)
        executable.chmod(0o700)
    completed = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            'set -Eeuo pipefail; enable -n test; source "$1"; '
            + invocation
            + '; printf "READY\\n"',
            "shell-test",
            str(helper),
        ],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "AEGIS_SHELL_TEST_STATE": str(state_file),
            "LC_ALL": "C",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed, json.loads(state_file.read_text())


def mutations(state: dict[str, Any]) -> list[list[str]]:
    return [row for row in state["calls"] if row[0] in ("chown", "chmod")]


def refused(completed: subprocess.CompletedProcess[str]) -> None:
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr == "database secret staging refused\n"


def test_empty_private_tmpfs_prepares_only_three_directories(tmp_path: Path) -> None:
    state = initial_state()
    completed, observed = run_preparation(tmp_path, state)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "READY\n"
    assert mutations(observed) == [
        ["chown", "70:70", "--", "/run/secrets"],
        ["chmod", "0700", "--", "/run/secrets"],
        ["chown", "70:70", "--", "/run/postgresql"],
        ["chmod", "0775", "--", "/run/postgresql"],
        ["chown", "70:70", "--", "/tmp"],
        ["chmod", "1777", "--", "/tmp"],
    ]
    for path, node in observed["nodes"].items():
        if path not in DESTINATIONS:
            assert node == state["nodes"][path]


def test_already_prepared_empty_directories_require_no_mutation(tmp_path: Path) -> None:
    state = initial_state()
    for path in DESTINATIONS:
        state["nodes"][path].update(uid=70, gid=70)
    completed, observed = run_preparation(tmp_path, state)
    assert completed.returncode == 0, completed.stderr
    assert mutations(observed) == []


@pytest.mark.parametrize("path", DESTINATIONS)
@pytest.mark.parametrize("entry", (".hidden", ".aegis-staged", "existing", ".s.PGSQL.5432"))
def test_any_existing_entry_prevents_every_mutation(
    tmp_path: Path,
    path: str,
    entry: str,
) -> None:
    state = initial_state()
    state["nodes"][path]["entries"].append(entry)
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []


@pytest.mark.parametrize("bad", ("uid", "gid", "mode", "kind", "missing"))
@pytest.mark.parametrize("path", ("/run", "/var/run", SOURCE, *DESTINATIONS))
def test_unknown_directory_state_never_gets_repaired(
    tmp_path: Path,
    path: str,
    bad: str,
) -> None:
    state = initial_state()
    if bad == "missing":
        del state["nodes"][path]
    else:
        state["nodes"][path][bad] = {
            "uid": 87,
            "gid": 88,
            "mode": 0o700 if path == "/var/run" else 0o777,
            "kind": stat.S_IFREG if path == "/var/run" else stat.S_IFLNK,
        }[bad]
    # Ancestor directories are not mutated; their image-owned metadata is also pinned.
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []


def test_root_at_the_measured_rootless_podman_mode_prepares_successfully(tmp_path: Path) -> None:
    state = initial_state()
    state["nodes"]["/"]["mode"] = 0o555
    completed, observed = run_preparation(tmp_path, state)
    assert completed.returncode == 0, completed.stderr
    assert mutations(observed) == [
        ["chown", "70:70", "--", "/run/secrets"],
        ["chmod", "0700", "--", "/run/secrets"],
        ["chown", "70:70", "--", "/run/postgresql"],
        ["chmod", "0775", "--", "/run/postgresql"],
        ["chown", "70:70", "--", "/tmp"],
        ["chmod", "1777", "--", "/tmp"],
    ]


@pytest.mark.parametrize(
    "mode", (0o775, 0o777, 0o1755, 0o2755, 0o4755, 0o511, 0o550)
)
def test_root_refuses_every_mode_other_than_the_two_measured_ones(
    tmp_path: Path, mode: int
) -> None:
    state = initial_state()
    state["nodes"]["/"]["mode"] = mode
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []


@pytest.mark.parametrize("uid,gid", ((0, 1), (1, 0)))
def test_root_refuses_a_non_root_owner_at_the_measured_podman_mode(
    tmp_path: Path, uid: int, gid: int
) -> None:
    state = initial_state()
    state["nodes"]["/"].update(mode=0o555, uid=uid, gid=gid)
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []


def test_root_refuses_a_non_directory_at_the_measured_podman_mode(tmp_path: Path) -> None:
    state = initial_state()
    state["nodes"]["/"].update(mode=0o555, kind=stat.S_IFLNK)
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []


@pytest.mark.parametrize("path", ("/var", "/run"))
def test_var_and_run_still_refuse_the_measured_root_mode(tmp_path: Path, path: str) -> None:
    state = initial_state()
    state["nodes"][path]["mode"] = 0o555
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []


def test_root_refusal_diagnostic_still_names_the_root_target(tmp_path: Path) -> None:
    # The standalone "/" ancestor check must run with $target=/, exactly as the old
    # combined loop did, so the bootstrap diagnostic's ${target-} attribution is
    # unchanged by the mode widening above.
    from tests.deployment import test_container_boundaries as boundary

    state = initial_state()
    state["nodes"]["/"]["mode"] = 0o775
    invocation = boundary.POSTGRES_BOOTSTRAP_DIAGNOSTIC + "\naegis_prepare_postgres_tmpfs"
    completed, observed = run_preparation(tmp_path, state, invocation=invocation)
    assert completed.returncode == 1 and completed.stdout == ""
    assert "database secret staging refused\n" in completed.stderr
    assert "bootstrap-refusal target=/\n" in completed.stderr
    assert mutations(observed) == []


@pytest.mark.parametrize("target", ("/run", "/tmp/run", "../../run", "../run/", "../run\n"))
def test_only_the_measured_socket_alias_is_accepted(tmp_path: Path, target: str) -> None:
    state = initial_state()
    state["nodes"]["/var/run"]["target"] = target
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []


@pytest.mark.parametrize("bad", ("symlink", "fifo", "empty", "large", "missing", "extra"))
def test_complete_source_layout_is_validated_before_preparation(tmp_path: Path, bad: str) -> None:
    state = initial_state()
    path = f"{SOURCE}/db_media_password"
    if bad in ("symlink", "fifo"):
        state["nodes"][path]["kind"] = stat.S_IFLNK if bad == "symlink" else stat.S_IFIFO
    elif bad in ("empty", "large"):
        state["nodes"][path]["size"] = 0 if bad == "empty" else 4097
    elif bad == "missing":
        state["nodes"][SOURCE]["entries"].remove("db_media_password")
    else:
        state["nodes"][SOURCE]["entries"].append(".unexpected")
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []


@pytest.mark.parametrize("uid,gid", ((70, 70), (0, 70), (70, 0), (1000, 1000)))
def test_preparation_requires_the_root_bootstrap_branch(tmp_path: Path, uid: int, gid: int) -> None:
    state = initial_state() | {"uid": uid, "gid": gid}
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []


@pytest.mark.parametrize(
    "bad",
    (
        "not-tmpfs",
        "subpath",
        "missing",
        "duplicate",
        "duplicate-id",
        "child",
        "writable-source",
        "missing-source-mount",
        "extra-source-mount",
        "read-only",
        "executable",
        "suid",
        "device",
        "shared",
        "large",
        "no-size",
        "duplicate-size",
        "wrong-device",
        "malformed",
        "unterminated",
        "oversized",
        "empty",
    ),
)
def test_ambiguous_or_unproved_mount_metadata_fails_before_mutation(
    tmp_path: Path,
    bad: str,
) -> None:
    state = initial_state()
    lines = state["mountinfo"].splitlines()
    index = 4  # Last destination: early valid roots must not have been changed.
    line = lines[index]
    match bad:
        case "not-tmpfs":
            lines[index] = line.replace("- tmpfs", "- ext4")
        case "subpath":
            lines[index] = line.replace(" / /tmp ", " /host /tmp ")
        case "missing":
            del lines[index]
        case "duplicate":
            lines.append(line.replace("103 ", "999 ", 1))
        case "duplicate-id":
            lines.append("103 1 0:55 / /other rw - tmpfs tmpfs rw")
        case "child":
            lines.append("999 103 0:55 / /tmp/child rw - tmpfs tmpfs rw")
        case "writable-source":
            lines[-1] = lines[-1].replace(" ro -", " rw -")
        case "missing-source-mount":
            del lines[-1]
        case "extra-source-mount":
            lines.append(f"999 100 0:55 / {SOURCE}/extra ro - tmpfs tmpfs rw")
        case "read-only":
            lines[index] = line.replace("rw,nosuid", "ro,nosuid")
        case "executable":
            lines[index] = line.replace(",noexec", "")
        case "suid":
            lines[index] = line.replace(",nosuid", "")
        case "device":
            lines[index] = line.replace(",nodev", "")
        case "shared":
            lines[index] = line.replace(" - ", " shared:9 - ")
        case "large":
            lines[index] = line.replace("size=16384k", "size=32768k")
        case "no-size":
            lines[index] = line.replace("size=16384k,", "")
        case "duplicate-size":
            lines[index] = line.replace("size=16384k", "size=16384k,size=16384k")
        case "wrong-device":
            lines[index] = line.replace("0:16", "0:987")
        case "malformed":
            lines.append("malformed")
        case "unterminated":
            pass
        case "oversized":
            lines.append("x" * 1_048_577)
        case "empty":
            lines = []
    state["mountinfo"] = "\n".join(lines) + ("" if bad == "unterminated" else "\n")
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []


@pytest.mark.parametrize("small,large", (("65536", "16777216"), ("64K", "16M")))
def test_equivalent_sizes_and_mapped_mount_ids_are_accepted(
    tmp_path: Path,
    small: str,
    large: str,
) -> None:
    state = initial_state()
    state["mountinfo"] = (
        state["mountinfo"]
        .replace("size=64k", f"size={small}")
        .replace(
            "size=16384k",
            f"size={large}",
        )
    )
    completed, _ = run_preparation(tmp_path, state)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("command", ("chown", "chmod"))
@pytest.mark.parametrize("path", DESTINATIONS)
@pytest.mark.parametrize("action", ("error", "interrupt", "wrong-state"))
def test_failed_or_interrupted_preparation_never_reaches_staging(
    tmp_path: Path,
    command: str,
    path: str,
    action: str,
) -> None:
    state = initial_state() | {"fail": {"command": command, "path": path, "action": action}}
    completed, _ = run_preparation(tmp_path, state)
    refused(completed)


@pytest.mark.parametrize("path", (SOURCE, *DESTINATIONS))
@pytest.mark.parametrize("occurrence", (1, 2, 3))
def test_stat_failure_stops_preparation(tmp_path: Path, path: str, occurrence: int) -> None:
    state = initial_state() | {"fail": {"command": "stat", "path": path, "occurrence": occurrence}}
    completed, _ = run_preparation(tmp_path, state)
    refused(completed)


@pytest.mark.parametrize("replacement", ("inode", "source", "contents", "mount"))
def test_replacement_between_validation_and_mutation_is_refused(
    tmp_path: Path,
    replacement: str,
) -> None:
    state = initial_state()
    change: dict[str, Any] = {"command": "stat", "path": DESTINATIONS[0], "occurrence": 2}
    match replacement:
        case "inode":
            change["node"] = {"ino": 999}
        case "source":
            change.update(target=f"{SOURCE}/db_media_password", node={"ino": 999})
        case "contents":
            change["node"] = {"entries": [".unexpected"]}
        case "mount":
            change["mountinfo"] = state["mountinfo"].replace("101 1 ", "999 1 ")
    state["changes"] = [change]
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []


def test_real_entrypoint_refuses_invalid_late_root_before_staging(tmp_path: Path) -> None:
    state = initial_state()
    state["nodes"]["/tmp"]["entries"] = [".stale"]
    wrapper = REPOSITORY / "deploy/postgres/entrypoint.sh"
    invocation = (
        # The entrypoint sources a fixed image path; only this test maps it to
        # the tracked helper. Kernel/stat utilities remain the same strict fakes.
        "source() { [[ $1 == /usr/local/libexec/aegis-postgres-private-tmpfs.sh ]] || exit 98; "
        'builtin source "' + str(HELPER) + '"; }; '
        'builtin source "' + str(wrapper) + '" postgres'
    )
    completed, observed = run_preparation(tmp_path, state, invocation=invocation)
    refused(completed)
    assert any(row[0] == "stat" and row[-1] == "/tmp" for row in observed["calls"])
    assert mutations(observed) == []


def role_init_mountinfo(state: dict[str, Any]) -> str:
    """Keep only the mount records the role-init fixture inspects (/ and /run/secrets)."""
    return (
        "\n".join(
            line
            for line in state["mountinfo"].splitlines()
            if line.split()[4] in ("/", "/run/secrets")
        )
        + "\n"
    )


def test_role_init_fixture_prepares_only_its_empty_private_secret_root(tmp_path: Path) -> None:
    state = initial_state()
    state["mountinfo"] = role_init_mountinfo(state)
    fixture = REPOSITORY / "tests/support/postgres-role-init-tmpfs.sh"
    completed, observed = run_preparation(
        tmp_path,
        state,
        invocation=f'source "{fixture}"; aegis_prepare_role_init_tmpfs',
    )
    assert completed.returncode == 0, completed.stderr
    assert mutations(observed) == [
        ["chown", "70:70", "--", "/run/secrets"],
        ["chmod", "0700", "--", "/run/secrets"],
    ]


def test_role_init_fixture_accepts_the_measured_rootless_podman_root_mode(
    tmp_path: Path,
) -> None:
    # O6 measured rootless Podman's read-only overlay root at 0555; R2 already
    # widened the production wrapper for this, and the fixture must match it.
    state = initial_state()
    state["nodes"]["/"]["mode"] = 0o555
    state["mountinfo"] = role_init_mountinfo(state)
    fixture = REPOSITORY / "tests/support/postgres-role-init-tmpfs.sh"
    completed, observed = run_preparation(
        tmp_path,
        state,
        invocation=f'source "{fixture}"; aegis_prepare_role_init_tmpfs',
    )
    assert completed.returncode == 0, completed.stderr
    assert mutations(observed) == [
        ["chown", "70:70", "--", "/run/secrets"],
        ["chmod", "0700", "--", "/run/secrets"],
    ]


def test_role_init_fixture_refuses_root_mode_outside_the_measured_set(
    tmp_path: Path,
) -> None:
    # The / guard runs first in the ancestor loop, so a bad / mode must refuse
    # before /run is ever inspected; /run stays at its valid 755 to attribute
    # the refusal to the / guard alone.
    state = initial_state()
    state["nodes"]["/"]["mode"] = 0o775
    state["mountinfo"] = role_init_mountinfo(state)
    fixture = REPOSITORY / "tests/support/postgres-role-init-tmpfs.sh"
    completed, observed = run_preparation(
        tmp_path,
        state,
        invocation=f'source "{fixture}"; aegis_prepare_role_init_tmpfs',
    )
    refused(completed)
    assert mutations(observed) == []
    assert ["stat", "-c", "%d:%i:%f:%u:%g:%a:%s", "/run"] not in observed["calls"]


def test_role_init_fixture_refuses_run_mode_outside_the_measured_set(
    tmp_path: Path,
) -> None:
    # / stays at its valid 755 (no alt_mode applies to /run), so this refusal
    # is attributable only to the /run guard; the recorded stat of /run proves
    # that guard was actually reached before refusing.
    state = initial_state()
    state["nodes"]["/run"]["mode"] = 0o555
    state["mountinfo"] = role_init_mountinfo(state)
    fixture = REPOSITORY / "tests/support/postgres-role-init-tmpfs.sh"
    completed, observed = run_preparation(
        tmp_path,
        state,
        invocation=f'source "{fixture}"; aegis_prepare_role_init_tmpfs',
    )
    refused(completed)
    assert mutations(observed) == []
    assert ["stat", "-c", "%d:%i:%f:%u:%g:%a:%s", "/run"] in observed["calls"]


@pytest.mark.parametrize("bad", ("bind", "volume", "missing", "size", "protections"))
def test_role_init_container_provenance_is_checked_before_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bad: str,
) -> None:
    from tests.deployment import test_postgres_role_init as fixture
    from tests.support.container_runtime import OwnedDirectResource

    resource = OwnedDirectResource("container", "owned-id", "aegis.test.owner", "owner")
    monkeypatch.setattr(fixture, "recover_owned_resource", lambda *args: resource)
    calls: list[tuple[str, ...]] = []
    declaration = "rw,noexec,nosuid,nodev,size=64k,mode=0700"
    info: dict[str, Any] = {
        "HostConfig": {"Tmpfs": {"/run/secrets": declaration}},
        "Mounts": [{"Type": "tmpfs", "Destination": "/run/secrets", "RW": True}],
    }
    if bad in ("bind", "volume"):
        info["Mounts"][0]["Type"] = bad
    elif bad == "missing":
        info["HostConfig"] = {"Tmpfs": {}}
    else:
        info["HostConfig"] = {
            "Tmpfs": {
                "/run/secrets": declaration.replace(
                    "size=64k" if bad == "size" else "noexec,",
                    "size=128k" if bad == "size" else "",
                )
            }
        }

    def command(*arguments: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps([info]), "")

    monkeypatch.setattr(fixture, "_docker", command)
    with pytest.raises(AssertionError):
        fixture._create_role_init_resource(
            "owned-name",
            "owner",
            tmp_path / "not-created.cid",
            (),
            [],
            (),
        )
    assert not any(call[0] == "start" for call in calls)


@pytest.mark.parametrize("represented", (False, True))
def test_private_tmpfs_provenance_accepts_both_engine_inspections(represented: bool) -> None:
    from tests.support.postgres_tmpfs import assert_private_tmpfs_provenance

    mounts = [{"Type": "tmpfs", "Destination": "/run/secrets", "RW": True}] if represented else []
    assert_private_tmpfs_provenance(
        {
            "HostConfig": {"Tmpfs": {"/run/secrets": "noexec,nosuid,nodev,size=64k,mode=0700"}},
            "Mounts": mounts,
        },
        {"/run/secrets": (65_536, 0o700)},
    )


@pytest.mark.parametrize(
    "bad", ("stale", "owner", "non-tmpfs", "child", "replace", "chown", "chmod")
)
def test_direct_fixture_refuses_unproved_state_before_server_start(
    tmp_path: Path, bad: str
) -> None:
    state = initial_state()
    if bad == "stale":
        state["nodes"]["/run/secrets"]["entries"] = [".stale"]
    elif bad == "owner":
        state["nodes"]["/run/secrets"]["uid"] = 72
    elif bad == "non-tmpfs":
        state["mountinfo"] = state["mountinfo"].replace("- tmpfs tmpfs", "- btrfs /dev/test")
    elif bad == "child":
        state["mountinfo"] += "900 101 0:50 / /run/secrets/child ro - btrfs /dev/test rw\n"
    elif bad == "replace":
        state["changes"] = [
            {"command": "stat", "path": "/run/secrets", "occurrence": 2, "node": {"ino": 999}}
        ]
    else:
        state["fail"] = {"command": bad, "path": "/run/secrets"}
    fixture = REPOSITORY / "tests/support/postgres-role-init-tmpfs.sh"
    completed, observed = run_preparation(
        tmp_path,
        state,
        invocation=f'source "{fixture}"; aegis_prepare_role_init_tmpfs',
    )
    refused(completed)
    if bad not in ("chown", "chmod"):
        assert mutations(observed) == []


@pytest.mark.parametrize("command", ("dd", "readlink", "find", "id"))
def test_observation_command_failures_never_reach_mutation(tmp_path: Path, command: str) -> None:
    state = initial_state() | {"fail": {"command": command}}
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []


def test_unreadable_last_source_prevents_every_directory_mutation(tmp_path: Path) -> None:
    state = initial_state()
    state["nodes"][f"{SOURCE}/db_media_password"]["readable"] = False
    completed, observed = run_preparation(tmp_path, state)
    refused(completed)
    assert mutations(observed) == []
