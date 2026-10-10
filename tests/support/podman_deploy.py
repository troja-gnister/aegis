"""Load the standalone Podman installer and read the Quadlet units it renders."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
INSTALLER = REPOSITORY / "deploy" / "podman" / "aegis-podman"
MODULE_NAME = "aegis_podman_installer"
QUADLET = Path("/usr/libexec/podman/quadlet")

# Synthetic install values; none of them names a real host, address or registry.
SAMPLE_PREFIX = "aegis-"
SAMPLE_RELEASE = "sha-" + "0123456789abcdef" * 2 + "01234567"
SAMPLE_PORT = 9443
SAMPLE_NAMES = ("aegis.example.test", "192.0.2.10")
SAMPLE_HOST_GID = 4321
SAMPLE_READER_TYPE = "aegis_reader.process"
SAMPLE_SECRETS_DIR = "/home/aegis-operator/.local/share/aegis/secrets"
SAMPLE_BACKEND_SUBNET = "10.123.45.0/24"
SAMPLE_IMAGES = {
    role: f"registry.example.test/aegis-{role}@sha256:{digit * 64}"
    for role, digit in (("backend", "a"), ("gateway", "b"), ("postgres", "c"), ("caddy", "d"))
}


def load_installer() -> ModuleType:
    """Import the extension-less installer once, the way the host's python3 runs it."""
    loaded = sys.modules.get(MODULE_NAME)
    if loaded is not None:
        return loaded
    loader = SourceFileLoader(MODULE_NAME, str(INSTALLER))
    spec = importlib.util.spec_from_loader(MODULE_NAME, loader)
    if spec is None:
        raise ImportError(f"cannot load {INSTALLER}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve string annotations through sys.modules during class creation.
    sys.modules[MODULE_NAME] = module
    try:
        loader.exec_module(module)
    except BaseException:
        del sys.modules[MODULE_NAME]
        raise
    return module


def sample_config(**overrides: Any) -> Any:
    installer = load_installer()
    fields: dict[str, Any] = {
        "prefix": SAMPLE_PREFIX,
        "release": SAMPLE_RELEASE,
        "https_port": SAMPLE_PORT,
        "names": SAMPLE_NAMES,
        "host_gid": SAMPLE_HOST_GID,
        "reader_type": SAMPLE_READER_TYPE,
        "secrets_dir": SAMPLE_SECRETS_DIR,
        "backend_subnet": SAMPLE_BACKEND_SUBNET,
        **{f"image_{role}": image for role, image in SAMPLE_IMAGES.items()},
    }
    return installer.UnitConfig(**(fields | overrides))


@dataclass(frozen=True)
class Unit:
    """One systemd unit file: each section keeps its keys in order, duplicates included."""

    sections: dict[str, list[tuple[str, str]]]

    def values(self, section: str, key: str) -> list[str]:
        return [value for name, value in self.sections.get(section, []) if name == key]

    def value(self, section: str, key: str) -> str | None:
        values = self.values(section, key)
        if len(values) > 1:
            raise AssertionError(f"[{section}] {key} is set {len(values)} times")
        return values[0] if values else None

    def keys(self, section: str) -> set[str]:
        return {name for name, _ in self.sections.get(section, [])}


def parse_unit(text: str) -> Unit:
    """Parse the unit-file subset the templates use; anything else fails loudly."""
    sections: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.endswith("\\"):
            raise AssertionError(f"line {number}: continuation lines are not used")
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1]
            if name in sections:
                raise AssertionError(f"line {number}: section [{name}] repeats")
            current = sections[name] = []
            continue
        key, separator, value = stripped.partition("=")
        if current is None or not separator or not key.strip():
            raise AssertionError(f"line {number}: not a key=value line in a section")
        current.append((key.strip(), value.strip()))
    return Unit(sections)


def systemd_words(command: str) -> list[str]:
    """Split a systemd command line the way systemd does for ExecStart= and Exec=."""
    escapes = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
               "\\": "\\", '"': '"', "'": "'", "s": " "}
    words: list[str] = []
    word: list[str] = []
    quote = ""
    started = False
    index = 0
    while index < len(command):
        character = command[index]
        if character == "\\" and index + 1 < len(command):
            following = command[index + 1]
            if following == "x":
                word.append(chr(int(command[index + 2:index + 4], 16)))
                index += 4
            else:
                word.append(escapes[following])
                index += 2
            started = True
            continue
        if quote:
            if character == quote:
                quote = ""
            else:
                word.append(character)
        elif character in "\"'":
            quote = character
            started = True
        elif character.isspace():
            if started:
                words.append("".join(word))
                word, started = [], False
        else:
            word.append(character)
            started = True
        index += 1
    if quote:
        raise AssertionError(f"unterminated quote in {command!r}")
    if started:
        words.append("".join(word))
    return words


def network_line(line: str) -> tuple[str, list[str], dict[str, str]]:
    """Split Network=<unit>.network:alias=a,alias=b,ip=x into reference, aliases, options."""
    reference, _, joined = line.partition(":")
    aliases: list[str] = []
    options: dict[str, str] = {}
    for option in joined.split(",") if joined else []:
        key, separator, value = option.partition("=")
        if not separator or (key != "alias" and key in options):
            raise AssertionError(f"Network={line} has a malformed or repeated option")
        if key == "alias":
            aliases.append(value)
        else:
            options[key] = value
    return reference, aliases, options


def environment_assignments(unit: Unit) -> dict[str, str]:
    """Read Environment= lines; each template line carries exactly one assignment."""
    assignments: dict[str, str] = {}
    for raw in unit.values("Container", "Environment"):
        words = systemd_words(raw)
        if len(words) != 1:
            raise AssertionError(f"Environment={raw} must hold one assignment")
        key, separator, value = words[0].partition("=")
        if not separator or key in assignments:
            raise AssertionError(f"Environment={raw} is malformed or repeats {key}")
        assignments[key] = value
    return assignments


def quadlet_dryrun(units: Mapping[str, str], directory: Path) -> subprocess.CompletedProcess[str]:
    """Run Podman's Quadlet generator over only these units; it writes nothing."""
    for name, text in units.items():
        (directory / name).write_text(text, encoding="utf-8")
    environment = dict(os.environ) | {"QUADLET_UNIT_DIRS": str(directory)}
    return subprocess.run(
        [str(QUADLET), "-user", "-dryrun"], env=environment, check=False,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )


def generated_services(output: str) -> dict[str, Unit]:
    """Split Quadlet's dry-run output into its generated services."""
    services: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in output.splitlines():
        if line.startswith("---") and line.endswith("---") and len(line) > 6:
            current = services[line[3:-3]] = []
        elif current is not None:
            current.append(line)
    return {name: parse_unit("\n".join(lines)) for name, lines in services.items()}


def exec_start_arguments(service: Unit) -> list[str]:
    """ExecStart= as argv, with each --flag=value split into two words."""
    command = service.value("Service", "ExecStart")
    if command is None:
        raise AssertionError("generated service has no ExecStart")
    arguments: list[str] = []
    for word in systemd_words(command):
        flag, separator, value = word.partition("=")
        if word.startswith("--") and separator:
            arguments.extend((flag, value))
        else:
            arguments.append(word)
    return arguments
