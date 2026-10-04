from __future__ import annotations

import re
import shutil
from itertools import pairwise
from pathlib import Path

import pytest

from scripts.podman_dev import webkit
from scripts.podman_dev.records import SessionError, WebkitRecord
from tests.support.podman_dev_doubles import ScriptedPodman

REPOSITORY = Path(__file__).resolve().parents[2]
CONTAINER = "c" * 64
IMAGE = "e" * 64
TOKEN = "0123456789abcdef" * 2
ENDPOINT = re.compile(r"ws://127\.0\.0\.1:([1-9][0-9]{3,4})/([0-9a-f]{32})")


def copy_image_inputs(target: Path) -> Path:
    for relative in webkit.IMAGE_INPUTS:
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / relative, target / relative)
    return target


def test_dockerfile_pins_bases_by_digest_and_reuses_the_locked_node() -> None:
    dockerfile = (REPOSITORY / "docker/dev-webkit.Dockerfile").read_text(encoding="utf-8")
    bases = re.findall(r"^FROM (\S+)", dockerfile, flags=re.MULTILINE)
    assert bases and all("@sha256:" in base for base in bases)
    lock = (REPOSITORY / "deploy/images.lock").read_text(encoding="utf-8").split("\n")
    node = next(line.split() for line in lock if line.startswith("node "))
    assert f"docker.io/library/node:{node[1]}@{node[2]}" in bases
    assert "mcr.microsoft.com/playwright:v1.63.0-noble@sha256:" in bases[-1]
    assert re.search(r"^USER pwuser$", dockerfile, flags=re.MULTILINE)


def test_create_arguments_lock_the_container_down() -> None:
    arguments = webkit.create_arguments(IMAGE, 41234, TOKEN)
    assert arguments[0] == "create"
    assert TOKEN[:12] not in arguments[arguments.index("--name") + 1]
    pairs = list(pairwise(arguments))
    assert ("--cap-drop", "ALL") in pairs
    assert ("--security-opt", "no-new-privileges:true") in pairs
    assert ("--network", "pasta:-T,18080,--map-guest-addr,none") in pairs
    assert ("--publish", "127.0.0.1:41234:41234") in pairs
    assert ("--memory", "2g") in pairs
    assert ("--pids-limit", "1024") in pairs
    assert "--read-only" in arguments
    for forbidden in ("--privileged", "--volume", "-v", "--mount", "--device", "host"):
        assert forbidden not in arguments
    assert arguments[arguments.index(IMAGE) + 1 :] == [
        "node", webkit.SERVER_CLI, "run-server", "--host", "0.0.0.0",
        "--port", "41234", "--path", f"/{TOKEN}",
    ]


def test_image_tag_follows_the_image_inputs(tmp_path: Path) -> None:
    repository = copy_image_inputs(tmp_path)
    first = webkit.image_tag(repository)
    assert re.fullmatch(r"localhost/aegis-dev-webkit:[0-9a-f]{16}", first)
    lock = repository / "frontend/package-lock.json"
    lock.write_text(lock.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert webkit.image_tag(repository) != first


def test_existing_image_is_reused_without_building(tmp_path: Path) -> None:
    repository = copy_image_inputs(tmp_path)
    run = ScriptedPodman([(("image", "inspect"), 0, f"{IMAGE}\n")])
    assert webkit.ensure_image(run, repository) == IMAGE
    run.assert_done()


def test_missing_image_is_built_once(tmp_path: Path) -> None:
    repository = copy_image_inputs(tmp_path)
    run = ScriptedPodman([
        (("image", "inspect"), 125, ""),
        (("build", "--file", str(repository / webkit.DOCKERFILE)), 0, "built"),
        (("image", "inspect"), 0, f"{IMAGE}\n"),
    ])
    assert webkit.ensure_image(run, repository) == IMAGE
    run.assert_done()


def started_replies(running: str = "true") -> list[tuple[tuple[str, ...], int, str]]:
    return [
        (("image", "inspect"), 0, f"{IMAGE}\n"),
        (("create",), 0, f"{CONTAINER}\n"),
        (("start", CONTAINER), 0, CONTAINER),
        (("container", "inspect", "--format", "{{.State.Running}}", CONTAINER), 0, running),
        (("logs", CONTAINER), 0, f"Listening on ws://0.0.0.0:41234/{TOKEN}\n"),
    ]


def test_start_records_the_container_before_starting_it(tmp_path: Path) -> None:
    repository = copy_image_inputs(tmp_path)
    run = ScriptedPodman(started_replies())
    seen: list[tuple[WebkitRecord, int]] = []
    record = webkit.start_webkit(
        lambda created: seen.append((created, len(run.calls))), run,
        repository=repository, port=41234, token=TOKEN,
    )
    assert record == WebkitRecord(CONTAINER, IMAGE, 41234, TOKEN)
    assert seen == [(record, 2)]
    assert ENDPOINT.fullmatch(record.endpoint)
    run.assert_done()


def test_failed_start_is_still_reported_for_cleanup(tmp_path: Path) -> None:
    repository = copy_image_inputs(tmp_path)
    run = ScriptedPodman([
        (("image", "inspect"), 0, f"{IMAGE}\n"),
        (("create",), 0, f"{CONTAINER}\n"),
        (("start", CONTAINER), 125, ""),
    ])
    seen: list[WebkitRecord] = []
    with pytest.raises(SessionError, match="starting the WebKit server failed"):
        webkit.start_webkit(seen.append, run, repository=repository, port=41234, token=TOKEN)
    assert seen == [WebkitRecord(CONTAINER, IMAGE, 41234, TOKEN)]


def test_container_that_stops_during_startup_fails(tmp_path: Path) -> None:
    repository = copy_image_inputs(tmp_path)
    run = ScriptedPodman(started_replies(running="false")[:4])
    with pytest.raises(SessionError, match="stopped during startup"):
        webkit.start_webkit(
            lambda created: None, run, repository=repository, port=41234, token=TOKEN,
        )


RECORD = WebkitRecord(CONTAINER, IMAGE, 41234, TOKEN)


def test_remove_accepts_an_already_absent_container() -> None:
    run = ScriptedPodman([(("container", "exists", CONTAINER), 1, "")])
    webkit.remove_webkit(RECORD, run)
    run.assert_done()


def test_remove_refuses_a_container_with_another_image() -> None:
    run = ScriptedPodman([
        (("container", "exists", CONTAINER), 0, ""),
        (("container", "inspect"), 0, f"{CONTAINER} {'9' * 64}\n"),
    ])
    with pytest.raises(SessionError, match="not the recorded WebKit server"):
        webkit.remove_webkit(RECORD, run)
    assert not any(call[0] == "rm" for call in run.calls)


def test_remove_deletes_by_full_id_and_confirms_absence() -> None:
    run = ScriptedPodman([
        (("container", "exists", CONTAINER), 0, ""),
        (("container", "inspect"), 0, f"{CONTAINER} {IMAGE}\n"),
        (("rm", "--force", "--time", "5", CONTAINER), 0, CONTAINER),
        (("container", "exists", CONTAINER), 1, ""),
    ])
    webkit.remove_webkit(RECORD, run)
    run.assert_done()


def test_running_check_requires_the_recorded_identity() -> None:
    healthy = ScriptedPodman([(("container", "inspect"), 0, f"{CONTAINER} {IMAGE} true\n")])
    assert webkit.webkit_running(RECORD, healthy)
    replaced = ScriptedPodman([(("container", "inspect"), 0, f"{CONTAINER} {'9' * 64} true")])
    assert not webkit.webkit_running(RECORD, replaced)
