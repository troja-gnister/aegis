"""The installer extracts ``aegisctl`` and runs it on the host's Python 3.11+, stdlib only."""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[4]
PACKAGE = REPOSITORY / "backend" / "aegisctl"
PREFIX = "test-aegis-"
DROP_IN = "50-aegis-roots.conf"
IMAGE = "registry.example.test/aegis/aegis-gateway@sha256:" + "b" * 64
HOST_PYTHON = r"""
import importlib.abc
import json
import sys

BLOCKED = ("yaml", "django", "aegis_apps", "aegis")


class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"host Python has no {name}")
        return None


sys.meta_path.insert(0, Refuse())
sys.path.insert(0, sys.argv[1])
arguments = json.loads(sys.argv[2])
from aegisctl.cli import main

status = 0
for command in arguments:
    status = status or main(command)
loaded = sorted(name for name in sys.modules if name.split(".")[0] in BLOCKED)
print(json.dumps({"status": status, "loaded": loaded}))
"""
# A stand-in for the podman CLI on PATH: it logs its argv and plays an observer that
# prints one mountinfo record per bind. It never starts anything.
FAKE_PODMAN = r"""#!{python} -S
import json, os, sys
arguments = sys.argv[1:]
with open(os.environ["FAKE_PODMAN_LOG"], "a", encoding="ascii") as log:
    log.write(json.dumps(arguments) + "\n")
if arguments[:1] != ["--remote=false"]:
    sys.exit(2)
arguments = arguments[1:]
if arguments[:2] == ["container", "exists"]:
    sys.exit(1)
if arguments[:1] == ["run"]:
    pairs = zip(arguments, arguments[1:])
    targets = [value.split(":")[1] for flag, value in pairs if flag == "-v"]
    for index, target in enumerate(targets):
        sys.stdout.write(
            f"64{{index}} 631 0:5{{index}} /private/{{index}} {{target}} ro "
            f"- fakeowner /run/host_mark/p{{index}} rw\n"
        )
    sys.exit(0)
sys.exit(2)
"""


def _library(tmp_path: Path) -> Path:
    library = tmp_path / "release-lib"
    shutil.copytree(PACKAGE, library / "aegisctl", ignore=shutil.ignore_patterns("__pycache__"))
    return library


def _host_command(
    library: Path, arguments: list[str], environment: dict[str, str], cwd: Path,
) -> subprocess.CompletedProcess[str]:
    # -S: no site-packages at all, so neither yaml nor Django can be imported.
    return subprocess.run(
        [sys.executable, "-S", "-m", "aegisctl", *arguments],
        env={**environment, "PYTHONPATH": str(library)}, cwd=cwd, check=False,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )


def test_quadlet_render_runs_on_a_standard_library_only_host_python(tmp_path: Path) -> None:
    from dataclasses import replace

    from aegisctl.mounts import local_identity, parse_config, preflight_slots, write_manifest

    source = tmp_path / "readonly-private"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    config.write_text(
        "version = 1\n[[slots]]\n"
        f'slot_id = "photos"\nsource = {json.dumps(str(source))}\n'
        'container_path = "/srv/aegis/roots/photos"\nmode = "read_only"\n'
        f'expected_identity = "{local_identity(source)}"\n',
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    validated = tuple(replace(slot, mount_fingerprint="a" * 64)
                      for slot in preflight_slots(parse_config(config)))
    write_manifest(manifest, validated, uid=os.geteuid(), gid=os.getegid())
    library = _library(tmp_path)
    output = tmp_path / "units"
    commands = [
        ["mounts", "validate", "--config", str(config)],
        ["mounts", "render", "--format", "quadlet", "--config", str(config),
         "--manifest", str(manifest), "--unit-prefix", PREFIX, "--output-dir", str(output),
         "--gateway-attestation", str(tmp_path / "gateway.attestation"),
         "--host-gid", str(os.getegid())],
    ]

    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", HOST_PYTHON, str(library), json.dumps(commands)],
        cwd=tmp_path, check=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {"status": 0, "loaded": []}
    assert f"Volume={source}:/srv/aegis/roots/photos:ro" in (
        output / f"{PREFIX}indexer.container.d" / DROP_IN
    ).read_text(encoding="ascii")


def test_roots_flow_runs_as_python_m_aegisctl_with_the_podman_run_observer(
    tmp_path: Path,
) -> None:
    source = tmp_path / "readonly-private"
    source.mkdir()
    library = _library(tmp_path)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    podman = binaries / "podman"
    podman.write_text(FAKE_PODMAN.format(python=sys.executable), encoding="ascii")
    podman.chmod(0o700)
    log = tmp_path / "podman.log"
    temporary = tmp_path / "tmp"
    temporary.mkdir(mode=0o700)
    # PATH holds only the stand-in, so no real podman can ever be reached.
    environment = {
        "PATH": str(binaries), "FAKE_PODMAN_LOG": str(log), "TMPDIR": str(temporary),
        "HOME": str(tmp_path), "AEGIS_CONTAINER_ENGINE": "podman",
    }
    config = tmp_path / "mounts.toml"
    manifest = tmp_path / "manifest.json"
    output = tmp_path / "units"

    inspected = _host_command(library, ["mounts", "inspect", "--source", str(source)],
                              environment, tmp_path)
    assert inspected.returncode == 0, inspected.stderr
    config.write_text(
        "version = 1\n[[slots]]\n"
        f'slot_id = "photos"\nsource = {json.dumps(str(source))}\n'
        'container_path = "/srv/aegis/roots/photos"\nmode = "read_only"\n'
        f"expected_identity = {json.dumps(inspected.stdout.strip())}\n",
        encoding="utf-8",
    )
    for arguments in (
        ["mounts", "validate", "--config", str(config)],
        ["mounts", "preflight", "--config", str(config), "--manifest", str(manifest),
         "--observer", "podman-run", "--observer-image", IMAGE,
         "--host-gid", str(os.getegid()), "--reader-type", "container_t"],
        ["mounts", "render", "--format", "quadlet", "--config", str(config),
         "--manifest", str(manifest), "--unit-prefix", PREFIX, "--output-dir", str(output),
         "--gateway-attestation", str(tmp_path / "gateway.attestation"),
         "--host-gid", str(os.getegid())],
    ):
        result = _host_command(library, arguments, environment, tmp_path)
        assert result.returncode == 0, (arguments[1], result.stderr)

    calls = [json.loads(line) for line in log.read_text(encoding="ascii").splitlines()]
    assert [call[1:3] for call in calls] == [
        ["container", "exists"], ["run", "--rm"], ["container", "exists"],
    ]
    run = calls[1]
    assert f"+g101:@{os.getegid()}:1" in run and "label=type:container_t" in run
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert not list(temporary.iterdir())
    assert f"Volume={source}:/srv/aegis/roots/photos:ro" in (
        output / f"{PREFIX}gateway.container.d" / DROP_IN
    ).read_text(encoding="ascii")


def test_mounts_module_imports_without_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.abc
    import importlib.machinery

    class RefuseYaml(importlib.abc.MetaPathFinder):
        def find_spec(
            self, name: str, path: object = None, target: object = None,
        ) -> importlib.machinery.ModuleSpec | None:
            del path, target
            if name == "yaml" or name.startswith("yaml."):
                raise ImportError("yaml is not available")
            return None

    for name in list(sys.modules):
        if name == "yaml" or name.startswith(("yaml.", "aegisctl")):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [RefuseYaml(), *sys.meta_path])

    import aegisctl.cli
    import aegisctl.mounts

    assert callable(aegisctl.mounts.render_quadlet_artifacts)
    assert callable(aegisctl.cli.main)
    assert "yaml" not in sys.modules


@pytest.mark.parametrize("path", sorted(PACKAGE.glob("*.py")), ids=lambda path: path.name)
def test_aegisctl_keeps_to_python_3_11_syntax(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    ast.parse(source, filename=str(path), feature_version=(3, 11))
    # feature_version does not catch PEP 701 f-strings (3.12): nested reuse of the
    # enclosing quote, or a backslash inside a replacement field.
    if not hasattr(tokenize, "FSTRING_START"):
        return
    problems: list[str] = []
    quotes: list[str] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type == tokenize.FSTRING_START:
                quote = token.string.lstrip("rRfFbB")
                if quotes and quote[0] == quotes[-1][0]:
                    problems.append(f"{token.start[0]}: nested f-string reuses its quote")
                quotes.append(quote)
            elif token.type == tokenize.FSTRING_END:
                quotes.pop()
            elif token.type == tokenize.STRING and quotes:
                if token.string.lstrip("rRbBuU")[0] == quotes[-1][0]:
                    problems.append(f"{token.start[0]}: string reuses the f-string's quote")
                if "\\" in token.string:
                    problems.append(f"{token.start[0]}: backslash in an f-string field")
    assert not problems, problems
