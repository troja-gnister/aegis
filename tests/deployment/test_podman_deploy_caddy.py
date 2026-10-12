"""Caddyfile.lan and the deployment files the images ship (spec D2, D4, D8).

The static tests read the repository. The engine-backed tests build the Caddy and backend
images once per module, each with a unique tag and owner label, and remove exactly the
recorded image IDs afterwards.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aegisctl.container_engine import ContainerEngineError

import tests.deployment.test_indexer_runtime as runtime
from tests.deployment.test_compose import CONTAINER_COMMAND, PYTHON_IMAGE, adapted_caddyfile
from tests.support.container_runtime import run_deployment_process as run_container
from tests.support.podman_deploy import (
    SAMPLE_PREFIX,
    environment_assignments,
    load_installer,
    parse_unit,
    sample_config,
    systemd_words,
)

REPOSITORY = Path(__file__).resolve().parents[2]
CADDY_DIRECTORY = REPOSITORY / "deploy" / "caddy"
LAN_CADDYFILE = CADDY_DIRECTORY / "Caddyfile.lan"
LAN_TARGET = "/etc/caddy/Caddyfile.lan"
PODMAN_SOURCE = REPOSITORY / "deploy" / "podman"
PODMAN_TARGET = "/usr/share/aegis/podman"
# Exactly what the sample install puts in the Caddy unit's environment. Test values only.
LAN_SITES = "https://aegis.example.test:9443, https://192.0.2.10:9443"
LAN_HOSTS = ("aegis.example.test", "192.0.2.10")
OWNER_LABEL = "aegis.podman-deploy.owner"
RUNTIME_USER = "10001:10001"
BACKEND_COMMAND = [
    "uvicorn", "aegis.asgi:application", "--host", "0.0.0.0", "--port", "8000",
    "--log-config", "/app/backend/aegis/uvicorn_logging.json",
]
# A throwaway probe never needs a network, a writable root or any capability.
PROBE_OPTIONS = (
    "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges:true",
)


# -- Caddyfile.lan, statically -------------------------------------------------------------

@dataclass(frozen=True)
class Directive:
    """One Caddyfile line: its words, and its block when it opens one."""

    words: tuple[str, ...]
    block: tuple[Directive, ...] | None = None


def parse_caddyfile(text: str) -> tuple[Directive, ...]:
    """Parse the Caddyfile subset the repository uses: one directive or brace per line."""
    stack: list[tuple[tuple[str, ...], list[Directive]]] = []
    current: list[Directive] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "}":
            if not stack:
                raise AssertionError(f"line {number}: unbalanced closing brace")
            words, parent = stack.pop()
            parent.append(Directive(words, tuple(current)))
            current = parent
            continue
        words = tuple(line.split())
        if words[-1] == "{":
            stack.append((words[:-1], current))
            current = []
        elif "{" in words or "}" in words:
            raise AssertionError(f"line {number}: a brace must end its line")
        else:
            current.append(Directive(words))
    if stack:
        raise AssertionError("unclosed Caddyfile block")
    return tuple(current)


def reverse_proxies(path: Path) -> list[Directive]:
    """Every site's reverse_proxy directive, with its block."""
    return [
        directive
        for site in parse_caddyfile(path.read_text(encoding="utf-8"))
        for directive in site.block or ()
        if directive.words[0] == "reverse_proxy"
    ]


def existing_reverse_proxy() -> Directive:
    """The upstream configuration that every compose Caddyfile site shares."""
    proxies = [
        proxy for name in ("Caddyfile", "Caddyfile.local")
        for proxy in reverse_proxies(CADDY_DIRECTORY / name)
    ]
    assert len(proxies) == 2
    assert all(proxy == proxies[0] for proxy in proxies)
    return proxies[0]


def test_lan_caddyfile_turns_admin_off_and_skips_trust_and_redirects() -> None:
    global_options, _ = parse_caddyfile(LAN_CADDYFILE.read_text(encoding="utf-8"))

    assert global_options == Directive((), (
        Directive(("admin", "off")),
        Directive(("auto_https", "disable_redirects")),
        Directive(("skip_install_trust",)),
    ))


def test_lan_caddyfile_serves_only_the_configured_sites_on_the_systemd_socket() -> None:
    _, site = parse_caddyfile(LAN_CADDYFILE.read_text(encoding="utf-8"))
    upstream = existing_reverse_proxy()

    # The installer forms every address as https://<name>:<port> (AEGIS_LAN_SITES).
    assert site.words == ("{$AEGIS_LAN_SITES}",)
    assert site.block == (
        # A stream socket cannot carry HTTP/3, so the fd listener speaks h1 and h2 only.
        Directive(("bind", "fd/3"), (Directive(("protocols", "h1", "h2")),)),
        Directive(("tls", "internal")),
        Directive(("reverse_proxy", "tls-gateway:8081"), upstream.block),
    )
    assert upstream == Directive(("reverse_proxy", "tls-gateway:8081"), (
        Directive(("header_up", "X-Forwarded-For", "{remote_host}")),
        Directive(("header_up", "X-Forwarded-Proto", "https")),
    ))


def test_lan_caddyfile_has_no_plain_http_site_and_no_fixed_host() -> None:
    text = LAN_CADDYFILE.read_text(encoding="utf-8")
    sites = parse_caddyfile(text)

    assert [site.words for site in sites] == [(), ("{$AEGIS_LAN_SITES}",)]
    assert re.search(r":80\b", text) is None
    for forbidden in ("http://", "h3", "AEGIS_TLS_HOST", "localhost", "acme"):
        assert forbidden not in text


def test_caddy_unit_serves_the_image_copy_with_the_sample_sites() -> None:
    installer = load_installer()
    units = installer.render_units(
        sample_config(), installer.load_templates(PODMAN_SOURCE / "quadlet"),
    )
    caddy = parse_unit(units[f"{SAMPLE_PREFIX}caddy.container"])
    command = systemd_words(caddy.value("Container", "Exec") or "")

    assert command[command.index("--config") + 1] == LAN_TARGET
    assert environment_assignments(caddy)["AEGIS_LAN_SITES"] == LAN_SITES


# -- Dockerfiles, statically ---------------------------------------------------------------

def dockerfile_lines(name: str) -> list[str]:
    return (REPOSITORY / "docker" / name).read_text(encoding="utf-8").splitlines()


def test_caddy_image_copies_caddyfile_lan_read_only_before_dropping_root() -> None:
    lines = dockerfile_lines("caddy.Dockerfile")
    copy = f"COPY --chmod=0444 deploy/caddy/Caddyfile.lan {LAN_TARGET}"

    assert lines.count(copy) == 1
    assert lines[-1] == f"USER {RUNTIME_USER}"
    assert lines.index(
        "COPY --chmod=0755 deploy/caddy/aegis-caddy-start /usr/local/bin/aegis-caddy-start"
    ) < lines.index(copy)


def test_backend_image_copies_podman_files_in_their_own_layer_after_the_app() -> None:
    lines = dockerfile_lines("backend.Dockerfile")
    stages = [index for index, line in enumerate(lines) if line.startswith("FROM ")]
    files_stage = lines.index(f"FROM {PYTHON_IMAGE} AS podman-files")
    final = lines[stages[-1]:]
    copy = "COPY --from=podman-files /usr/share/aegis /usr/share/aegis"

    assert files_stage in stages[:-1]
    stage = lines[files_stage:stages[stages.index(files_stage) + 1]]
    assert f"COPY deploy/podman {PODMAN_TARGET}" in stage
    assert final.count(copy) == 1
    assert final.index("COPY --from=build /app/backend /app/backend") + 1 == final.index(copy)
    assert final.index(copy) + 1 == final.index(f"USER {RUNTIME_USER}")
    assert final[-1] == "CMD " + json.dumps(BACKEND_COMMAND)


def test_build_context_excludes_bytecode_but_keeps_the_deployment_files() -> None:
    patterns = (REPOSITORY / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert {"**/__pycache__", "**/*.pyc"} <= set(patterns)
    assert [pattern for pattern in patterns if pattern.startswith("deploy")] == [
        "deploy/secrets",
        "deploy/mounts.*.toml",
        "deploy/mounts.manifest.json",
        "deploy/mounts.gateway.attestation",
    ]


# -- Built images (engine-backed) ----------------------------------------------------------

def _require_owned_image(identity: str, tag: str) -> dict[str, Any]:
    """Refuse a replaced image, owner-label drift or a rebound unique tag."""
    info = runtime._inspect_one("image", "inspect", identity)
    if runtime._image_identity(info.get("Id")) != identity:
        raise ContainerEngineError("owned test image was replaced")
    if ((info.get("Config") or {}).get("Labels") or {}).get(OWNER_LABEL) != tag:
        raise ContainerEngineError("owned test image ownership changed")
    tagged = runtime._inspect_one("image", "inspect", tag)
    if runtime._image_identity(tagged.get("Id")) != identity:
        raise ContainerEngineError("owned test image tag was rebound")
    return info


def _remove_owned_image(identity: str, tag: str) -> None:
    """Validate the recorded ID, label and tag, then remove exactly that ID and prove it."""
    if not identity:
        # Unknown creation: never adopt an image found by its reusable tag or label.
        if runtime._listed_images("--filter", f"reference={tag}"):
            raise ContainerEngineError("test image identity was never recorded; tag retained")
        return
    _require_owned_image(identity, tag)
    if runtime._engine("image", "rm", "--no-prune", identity).returncode:
        raise ContainerEngineError("exact owned test image removal failed")
    if identity in runtime._listed_images("--all") or runtime._listed_images(
        "--filter", f"reference={tag}",
    ):
        raise ContainerEngineError("owned test image absence is unproved")


def _owned_image(dockerfile: str, factory: pytest.TempPathFactory) -> Iterator[str]:
    """Build one image under a unique tag and label; yield and finally remove its ID."""
    tag = f"aegis-podman-deploy-{uuid.uuid4().hex}"
    iidfile = factory.mktemp("podman-deploy-image") / "image.iid"
    identity = ""
    primary: BaseException | None = None
    try:
        try:
            built = run_container(
                [*CONTAINER_COMMAND, "build", "--iidfile", str(iidfile), "--tag", tag,
                 "--label", f"{OWNER_LABEL}={tag}", "--file", f"docker/{dockerfile}", "."],
                cwd=REPOSITORY, capture_output=True, text=True, timeout=900, check=False,
            )
        finally:
            # The engine-written ID is recorded even when the build call is interrupted.
            identity = runtime._read_iidfile(iidfile)
        assert built.returncode == 0, built.stderr[-5000:]
        assert identity, "built image identity was not recorded"
        _require_owned_image(identity, tag)
        yield identity
    except BaseException as exc:
        primary = exc
        raise
    finally:
        runtime._cleanup_preserving_failure(
            primary, lambda: _remove_owned_image(identity, tag),
        )


@pytest.fixture(scope="module")
def caddy_image(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _owned_image("caddy.Dockerfile", tmp_path_factory)


@pytest.fixture(scope="module")
def backend_image(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _owned_image("backend.Dockerfile", tmp_path_factory)


def _probe(image: str, *command: str, stdin: str | None = None) -> str:
    result = run_container(
        [*CONTAINER_COMMAND, "run", *PROBE_OPTIONS,
         *(["--interactive"] if stdin is not None else []), image, *command],
        input=stdin, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return result.stdout


def _handlers(node: object, name: str) -> list[dict[str, Any]]:
    """Every handler object named ``name``, at any depth (Caddy nests them in subroutes)."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("handler") == name:
            found.append(node)
        for value in node.values():
            found.extend(_handlers(value, name))
    elif isinstance(node, list):
        for value in node:
            found.extend(_handlers(value, name))
    return found


def test_lan_caddyfile_adapts_in_the_built_caddy_image(caddy_image: str) -> None:
    adapted = adapted_caddyfile(
        None, image=caddy_image, config=LAN_TARGET,
        environment={"AEGIS_LAN_SITES": LAN_SITES},
    )
    reference = adapted_caddyfile(CADDY_DIRECTORY / "Caddyfile.local")
    rendered = json.dumps(adapted, sort_keys=True)

    assert adapted["admin"] == {"disabled": True}
    servers = adapted["apps"]["http"]["servers"]
    listening = [value for value in servers.values() if "listen" in value]
    # The global disable_redirects option also yields one listener-free placeholder server
    # (Caddy 2.11's adapter); anything else would be a second, unintended listener.
    placeholders = [value for value in servers.values() if "listen" not in value]
    assert placeholders in ([], [{"automatic_https": {"disable_redirects": True}}])
    (server,) = listening
    assert server["listen"] == ["fd/3"]
    assert server["listen_protocols"] == [["h1", "h2"]]
    assert "protocols" not in server
    assert "h3" not in rendered
    assert server["automatic_https"]["disable_redirects"] is True
    (route,) = server["routes"]
    (matcher,) = route["match"]
    assert list(matcher) == ["host"]
    assert sorted(matcher["host"]) == sorted(LAN_HOSTS)

    policies = adapted["apps"]["tls"]["automation"]["policies"]
    for host in LAN_HOSTS:
        (policy,) = [policy for policy in policies if host in policy.get("subjects", [])]
        assert policy["issuers"] == [{"module": "internal"}]
    assert "acme" not in rendered
    assert adapted["apps"]["pki"]["certificate_authorities"]["local"]["install_trust"] is False

    (proxy,) = _handlers(server, "reverse_proxy")
    (expected,) = _handlers(reference, "reverse_proxy")
    assert proxy == expected
    assert proxy["upstreams"] == [{"dial": "tls-gateway:8081"}]
    assert list(proxy["headers"]) == ["request"]
    assert list(proxy["headers"]["request"]) == ["set"]
    assert set(proxy["headers"]["request"]["set"]) == {"X-Forwarded-For", "X-Forwarded-Proto"}
    assert proxy["headers"]["request"]["set"]["X-Forwarded-Proto"] == ["https"]


def test_caddy_image_ships_caddyfile_lan_read_only(caddy_image: str) -> None:
    info = runtime._inspect_one("image", "inspect", caddy_image)

    assert (info.get("Config") or {}).get("User") == RUNTIME_USER
    assert _probe(caddy_image, "stat", "-c", "%F %a %u:%g", LAN_TARGET) == (
        "regular file 444 0:0\n"
    )
    assert _probe(caddy_image, "cat", LAN_TARGET) == LAN_CADDYFILE.read_text(encoding="utf-8")


TREE_PROBE = r"""
import json, os, stat
def describe(path):
    status = os.lstat(path)
    kind = ("dir" if stat.S_ISDIR(status.st_mode)
            else "file" if stat.S_ISREG(status.st_mode) else "other")
    return [kind, stat.S_IMODE(status.st_mode), status.st_uid, status.st_gid]
root = "/usr/share/aegis/podman"
entries = {".": describe(root)}
for directory, directories, files in os.walk(root):
    for name in directories + files:
        path = os.path.join(directory, name)
        entries[os.path.relpath(path, root)] = describe(path)
print(json.dumps({"parent": describe(os.path.dirname(root)), "entries": entries}))
"""

RENDER_PROBE = r"""
import importlib.util, json, sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
path = Path("/usr/share/aegis/podman/aegis-podman")
loader = SourceFileLoader("aegis_podman_installer", str(path))
module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
sys.modules[loader.name] = module
loader.exec_module(module)
fields = {key: tuple(value) if isinstance(value, list) else value
          for key, value in json.load(sys.stdin).items()}
templates = module.load_templates(path.parent / "quadlet")
print(json.dumps(module.render_units(module.UnitConfig(**fields), templates)))
"""


def expected_podman_tree() -> dict[str, list[Any]]:
    """The build context's deploy/podman/ (bytecode is dockerignored), root-owned, read-only."""
    entries: dict[str, list[Any]] = {".": ["dir", 0o555, 0, 0]}
    for directory, directories, files in os.walk(PODMAN_SOURCE):
        directories[:] = [name for name in directories if name != "__pycache__"]
        for name in directories:
            relative = os.path.relpath(os.path.join(directory, name), PODMAN_SOURCE)
            entries[relative] = ["dir", 0o555, 0, 0]
        for name in files:
            if name.endswith(".pyc"):
                continue
            relative = os.path.relpath(os.path.join(directory, name), PODMAN_SOURCE)
            entries[relative] = ["file", 0o555 if relative == "aegis-podman" else 0o444, 0, 0]
    return entries


def test_backend_image_ships_the_podman_files_root_owned_and_read_only(
    backend_image: str,
) -> None:
    info = runtime._inspect_one("image", "inspect", backend_image)
    config = info.get("Config") or {}
    # The image's system Python, outside the app's virtualenv: the standard library only.
    tree = json.loads(_probe(backend_image, "/usr/local/bin/python3", "-I", "-B", "-c",
                             TREE_PROBE))

    assert config.get("User") == RUNTIME_USER
    assert config.get("Cmd") == BACKEND_COMMAND
    assert config.get("WorkingDir") == "/app/backend"
    assert tree["entries"] == expected_podman_tree()
    assert {"aegis-podman", "quadlet"} <= set(tree["entries"])
    kind, mode, uid, gid = tree["parent"]
    assert (kind, uid, gid) == ("dir", 0, 0)
    assert mode & 0o022 == 0


def test_backend_image_installer_renders_its_own_templates(backend_image: str) -> None:
    installer = load_installer()
    config = sample_config()
    expected = installer.render_units(
        config, installer.load_templates(PODMAN_SOURCE / "quadlet"),
    )

    # As in the D4 bootstrap, the installer runs as the image user, with no virtualenv.
    rendered = json.loads(_probe(
        backend_image, "/usr/local/bin/python3", "-I", "-B", "-c", RENDER_PROBE,
        stdin=json.dumps(dataclasses.asdict(config)),
    ))

    assert rendered == expected
    assert f"{SAMPLE_PREFIX}caddy.container" in rendered
