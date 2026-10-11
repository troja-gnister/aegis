"""Image hygiene (M9): images take only tracked sources, and the build context drops local data.

Every docker/*.Dockerfile is parsed statically. COPY and ADD may read the build context only
through an allowlist of tracked source paths; `COPY --from=` reads a stage or an image instead and
is exempt. The root .dockerignore must cover local-only material at any depth without excluding
anything a Dockerfile copies.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
DOCKERIGNORE = REPOSITORY / ".dockerignore"
KNOWN_DOCKERFILES = {"backend", "caddy", "dev-webkit", "gateway", "postgres"}

# Directories a Dockerfile may copy whole or in part.
ALLOWED_TREES = (
    "backend/aegis",
    "backend/aegis_apps",
    "backend/aegisctl",
    "backend/aegis_media_processor",
    "deploy/podman",
    "deploy/caddy",
    "deploy/nginx",
    "deploy/postgres",
)
# Sources a Dockerfile may copy only as written: files, a glob over sibling files, and the
# frontend tree, which only the gateway's build stage takes whole (its image keeps just dist/).
ALLOWED_EXACT = (
    "pyproject.toml",
    "uv.lock",
    "backend/manage.py",
    "frontend",
    "frontend/package*.json",
)
# The media processor package is allowlisted ahead of its arrival: it lands together with
# docker/media.Dockerfile and the backend image's copy of it. Until then no Dockerfile copies it,
# so only its tracked check is skipped, and only while the package is missing from the checkout.
PENDING_SOURCES = frozenset({"backend/aegis_media_processor"})

BASELINE_PATTERNS = (
    ".git", ".github", ".superpowers", ".worktrees", ".env", ".env.*", "!.env.example", ".venv",
    "**/__pycache__", "**/*.pyc", "**/.pytest_cache", "**/.mypy_cache", "**/.ruff_cache",
    "deploy/secrets", "frontend/node_modules", "frontend/dist", "frontend/test-results",
    "frontend/playwright-report", "backend/staticfiles", "tests", "docs", "fixtures",
)
LOCAL_ONLY_PATTERNS = (
    "**/.env", "**/.env.*", "!**/.env.example",
    "deploy/mounts.*.toml", "deploy/mounts.manifest.json", "deploy/mounts.gateway.attestation",
    "compose.mounts.generated.yaml",
    "**/test-results", "**/playwright-report", "**/blob-report", "**/coverage", "**/*.log",
)
LOCAL_ONLY_SAMPLES = (
    ".env", "backend/.env", "frontend/.env.local", "backend/aegis/settings/.env.production",
    ".superpowers/notes/session.md", "deploy/secrets/dev/token",
    "deploy/mounts.local.toml", "deploy/mounts.manifest.json",
    "deploy/mounts.gateway.attestation", "compose.mounts.generated.yaml",
    "test-results/run/trace.zip", "frontend/test-results/run/trace.zip",
    "frontend/playwright-report/index.html", "frontend/blob-report/report.zip",
    "coverage/lcov.info", "frontend/coverage/index.html", "backend/aegis_apps/coverage/data",
    "server.log", "backend/aegis/debug.log", "fixtures/media/sample.jpg",
)
KEPT_SAMPLES = (
    ".env.example", "frontend/.env.example", "backend/aegis/settings/base.py",
    "deploy/podman/aegis-podman", "deploy/caddy/Caddyfile.lan", "frontend/src/main.ts",
    "frontend/package.json", "pyproject.toml",
)

FLAG = re.compile(r"--([A-Za-z][A-Za-z0-9-]*)(?:=(\S*))?(?:\s+|$)")
CONTINUATION = re.compile(r"\\[ \t]*$")
ESCAPE_DIRECTIVE = re.compile(r"#\s*escape\s*=", re.IGNORECASE)
HEREDOC = re.compile(r"(?<!<)<<-?[\"']?[A-Za-z_]")
GLOB_CHARACTERS = re.compile(r"[*?\[\\]")
URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://|git@")


@dataclass(frozen=True)
class Instruction:
    origin: str
    keyword: str
    flags: tuple[tuple[str, str | None], ...]
    arguments: str

    def flag_values(self, name: str) -> list[str | None]:
        return [value for flag, value in self.flags if flag == name]


def parse_instruction(line: str, origin: str) -> Instruction:
    keyword, _, rest = line.partition(" ")
    keyword, rest = keyword.upper(), rest.strip()
    if keyword in {"ADD", "COPY", "RUN"} and HEREDOC.search(rest):
        raise ValueError(f"{origin}: heredocs are not modelled by this check")
    flags: list[tuple[str, str | None]] = []
    while match := FLAG.match(rest):
        flags.append((match[1].lower(), match[2]))
        rest = rest[match.end():]
    return Instruction(origin, keyword, tuple(flags), rest)


def parse_dockerfile(text: str, name: str) -> list[Instruction]:
    """Logical instructions as BuildKit reads them: continuations joined, comment lines dropped."""
    instructions: list[Instruction] = []
    pending: list[str] = []
    start = 0
    for number, line in enumerate(text.replace("\t", " ").splitlines(), start=1):
        if ESCAPE_DIRECTIVE.match(line.strip()):
            raise ValueError(f"{name}:{number}: parser directives are not modelled by this check")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not pending:
            start, line = number, line.lstrip()
        content, continued = CONTINUATION.subn("", line)
        pending.append(content)
        if not continued:
            instructions.append(parse_instruction("".join(pending), f"{name}:{start}"))
            pending = []
    if pending:
        raise ValueError(f"{name}:{start}: the last instruction ends in a continuation")
    return instructions


def copy_arguments(instruction: Instruction) -> tuple[list[str], str]:
    """Sources and destination of a COPY or ADD, in shell or JSON-array form."""
    rest = instruction.arguments
    if rest.startswith("["):
        words = json.loads(rest)
        if not isinstance(words, list) or not all(isinstance(word, str) for word in words):
            raise ValueError(f"{instruction.origin}: the JSON form must be a list of strings")
    else:
        words = rest.split()
    if len(words) < 2:
        raise ValueError(f"{instruction.origin}: {instruction.keyword} needs a source and a target")
    return words[:-1], words[-1]


def context_sources(instruction: Instruction) -> list[str]:
    """Build-context sources of a COPY or ADD; `--from=` reads a stage or an image instead."""
    if instruction.keyword not in {"ADD", "COPY"} or any(instruction.flag_values("from")):
        return []
    return copy_arguments(instruction)[0]


def context_mounts(instruction: Instruction) -> list[str]:
    """RUN bind mounts without `from=` read the build context, bypassing the COPY allowlist."""
    if instruction.keyword != "RUN":
        return []
    found: list[str] = []
    for value in instruction.flag_values("mount"):
        parts = (part.partition("=") for part in (value or "").split(","))
        options = {key: setting for key, _, setting in parts}
        if options.get("type", "bind") == "bind" and not options.get("from"):
            found.append(value or "")
    return found


def is_url(source: str) -> bool:
    return URL.match(source) is not None


def url_sources(instructions: Iterable[Instruction]) -> list[str]:
    return [
        f"{instruction.origin}: {source}"
        for instruction in instructions
        if instruction.keyword == "ADD"
        for source in copy_arguments(instruction)[0]
        if is_url(source)
    ]


def context_path(source: str) -> str:
    """BuildKit resolves `/x` and `./x` against the context root."""
    return posixpath.normpath(source.lstrip("/"))


@cache
def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Docker's pattern syntax (moby/patternmatcher): `*` and `?` within a name, `**` across."""
    if "[" in pattern or "\\" in pattern:
        raise ValueError(f"{pattern!r} uses syntax this check does not model")
    regex = []
    for token in re.finditer(r"\*\*/?|[*?]|[^*?]+", pattern):
        text = token.group()
        if text.startswith("**"):
            regex.append(".*" if token.end() == len(pattern) else "(?:.*/)?")
        elif text == "*":
            regex.append("[^/]*")
        elif text == "?":
            regex.append("[^/]")
        else:
            regex.append(re.escape(text))
    return re.compile("".join(regex))


def matches_or_parent(pattern: re.Pattern[str], path: str) -> bool:
    parts = path.split("/")
    return any(pattern.fullmatch("/".join(parts[:end])) for end in range(1, len(parts) + 1))


def admitted(source: str) -> bool:
    """Whether a context source stays inside the allowlist; anything unusual is refused."""
    if "$" in source or is_url(source):
        return False
    path = context_path(source)
    if any(path == tree or path.startswith(f"{tree}/") for tree in ALLOWED_TREES):
        return True
    if path in ALLOWED_EXACT:
        return True
    return not GLOB_CHARACTERS.search(path) and any(
        compile_pattern(entry).fullmatch(path) for entry in ALLOWED_EXACT
    )


@cache
def tracked_files() -> frozenset[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPOSITORY, check=True, capture_output=True,
    ).stdout
    return frozenset(os.fsdecode(path) for path in listing.split(b"\0") if path)


def tracked_matches(source: str) -> list[str]:
    """Tracked files a context source reaches: the file itself, or everything under a directory."""
    pattern = compile_pattern(context_path(source))
    return sorted(path for path in tracked_files() if matches_or_parent(pattern, path))


def ignore_patterns() -> list[str]:
    """The .dockerignore patterns as BuildKit reads them: comments and blanks dropped, cleaned."""
    patterns = []
    for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        negation = "!" if line.strip().startswith("!") else ""
        patterns.append(negation + context_path(line.strip().removeprefix("!").strip()))
    return patterns


def excluded(path: str, patterns: Sequence[str]) -> bool:
    """Docker's verdict on one context path: the last pattern matching it or a parent wins."""
    verdict = False
    for pattern in patterns:
        negation = pattern.startswith("!")
        if negation == verdict and matches_or_parent(
            compile_pattern(pattern.removeprefix("!")), path,
        ):
            verdict = not negation
    return verdict


def repository_instructions() -> list[Instruction]:
    dockerfiles = sorted((REPOSITORY / "docker").glob("*.Dockerfile"))
    names = {path.name.removesuffix(".Dockerfile") for path in dockerfiles}
    assert names >= KNOWN_DOCKERFILES
    return [
        instruction
        for path in dockerfiles
        for instruction in parse_dockerfile(
            path.read_text(encoding="utf-8"), path.relative_to(REPOSITORY).as_posix(),
        )
    ]


# -- Dockerfile parsing ---------------------------------------------------------------------

def test_parser_reads_every_copy_form() -> None:
    text = "\n".join([
        "# A comment before the first instruction.",
        "FROM base AS build",
        "copy pyproject.toml uv.lock ./",
        "COPY --chmod=0444 --chown=10001:10001 deploy/caddy/Caddyfile.lan \\",
        "    # A comment inside a continuation.",
        "    deploy/nginx/nginx.conf /etc/",
        'COPY --chown=10001:10001 ["backend/aegis", "backend/aegis_apps", "/app/"]',
        "COPY --from=build /app /app",
        "COPY --link --from=docker.io/library/base:1 /etc/os-release /etc/",
        "ADD --chmod=0555 deploy/postgres/init/ /init/",
    ])

    parsed = parse_dockerfile(text, "synthetic.Dockerfile")

    assert [(instruction.keyword, context_sources(instruction)) for instruction in parsed] == [
        ("FROM", []),
        ("COPY", ["pyproject.toml", "uv.lock"]),
        ("COPY", ["deploy/caddy/Caddyfile.lan", "deploy/nginx/nginx.conf"]),
        ("COPY", ["backend/aegis", "backend/aegis_apps"]),
        ("COPY", []),
        ("COPY", []),
        ("ADD", ["deploy/postgres/init/"]),
    ]
    assert parsed[2].origin == "synthetic.Dockerfile:4"
    assert parsed[2].flags == (("chmod", "0444"), ("chown", "10001:10001"))
    assert copy_arguments(parsed[3]) == (["backend/aegis", "backend/aegis_apps"], "/app/")


@pytest.mark.parametrize("text", [
    "# escape=`\nFROM base",
    "COPY <<EOF /etc/motd\nhello\nEOF",
    "RUN <<EOF\nset -e\nEOF",
    "COPY backend/aegis \\",
    'COPY ["backend/aegis", "/app/"',
    'COPY [1, "/app/"]',
    "COPY /only-a-target",
])
def test_parser_refuses_what_it_cannot_check(text: str) -> None:
    with pytest.raises(ValueError):
        for instruction in parse_dockerfile(text, "synthetic.Dockerfile"):
            context_sources(instruction)


@pytest.mark.parametrize("line", [
    "COPY . /app",
    "COPY ./ /app",
    "COPY deploy/mounts.local.toml /etc/aegis/",
    "COPY deploy/secrets /run/secrets",
    "COPY backend/aegis deploy/secrets /app/",
    'COPY ["fixtures/media", "/srv/"]',
    "COPY --chown=10001:10001 docs/ /docs/",
    "COPY backend/aegis/../../.env /app/",
    "COPY /deploy/mounts.manifest.json /run/",
    "COPY backend/aegis* /app/",
    "COPY frontend/src /app/",
    "COPY frontend/*.json /app/",
    "COPY $SOURCE /app/",
    "COPY --from= deploy/secrets /run/",
    "ADD https://example.com/archive.tar.gz /opt/",
    "ADD git@example.com:owner/repository.git /opt/",
])
def test_sources_outside_the_allowlist_are_refused(line: str) -> None:
    (instruction,) = parse_dockerfile(line, "synthetic.Dockerfile")
    sources = context_sources(instruction)

    assert sources
    assert not all(admitted(source) for source in sources)


# -- The repository's Dockerfiles -----------------------------------------------------------

def test_copy_and_add_take_only_allowlisted_sources() -> None:
    refused = [
        f"{instruction.origin}: {source}"
        for instruction in repository_instructions()
        for source in context_sources(instruction)
        if not admitted(source)
    ]

    assert refused == []


def test_add_never_fetches_a_url() -> None:
    synthetic = parse_dockerfile("ADD https://example.com/a.tar /opt/", "synthetic.Dockerfile")

    assert url_sources(synthetic) == ["synthetic.Dockerfile:1: https://example.com/a.tar"]
    assert url_sources(repository_instructions()) == []


def test_run_never_bind_mounts_the_build_context() -> None:
    synthetic = parse_dockerfile("\n".join([
        "RUN --mount=type=bind,source=deploy,target=/mnt true",
        "RUN --mount=target=/mnt,ro true",
        "RUN --mount=type=cache,target=/root/.cache "
        "--mount=type=bind,from=build,source=/app,target=/app true",
    ]), "synthetic.Dockerfile")

    assert [bool(context_mounts(instruction)) for instruction in synthetic] == [True, True, False]
    assert [
        f"{instruction.origin}: {mount}"
        for instruction in repository_instructions()
        for mount in context_mounts(instruction)
    ] == []


@pytest.mark.parametrize("entry", (*ALLOWED_TREES, *ALLOWED_EXACT))
def test_allowlisted_sources_are_tracked(entry: str) -> None:
    if entry in PENDING_SOURCES and not (REPOSITORY / entry).exists():
        pytest.skip(f"{entry} is allowlisted ahead of the package itself")
    untracked = [
        path.relative_to(REPOSITORY).as_posix()
        for path in REPOSITORY.glob(entry)
        if path.is_file() and path.relative_to(REPOSITORY).as_posix() not in tracked_files()
    ]

    assert tracked_matches(entry), f"{entry} reaches no tracked file"
    assert untracked == []


# -- The build context ----------------------------------------------------------------------

def test_dockerignore_has_every_required_pattern() -> None:
    patterns = ignore_patterns()

    assert [p for p in (*BASELINE_PATTERNS, *LOCAL_ONLY_PATTERNS) if p not in patterns] == []


def test_dockerignore_drops_local_only_material_at_any_depth() -> None:
    patterns = ignore_patterns()

    assert [path for path in LOCAL_ONLY_SAMPLES if not excluded(path, patterns)] == []
    assert [path for path in KEPT_SAMPLES if excluded(path, patterns)] == []


def test_dockerignore_keeps_everything_the_dockerfiles_copy() -> None:
    """An excluded source fails the build, or silently drops a file from the image."""
    patterns = ignore_patterns()
    unreached: list[str] = []
    dropped: list[str] = []
    for instruction in repository_instructions():
        for source in context_sources(instruction):
            files = tracked_matches(source)
            if not files:
                unreached.append(f"{instruction.origin}: {source}")
            dropped += [
                f"{instruction.origin}: {path}" for path in files if excluded(path, patterns)
            ]

    assert unreached == []
    assert dropped == []


def test_the_root_dockerignore_is_the_only_ignore_file() -> None:
    """Podman prefers .containerignore, and BuildKit a per-Dockerfile ignore file, over it."""
    assert DOCKERIGNORE.is_file()
    assert not (REPOSITORY / ".containerignore").exists()
    assert sorted((REPOSITORY / "docker").glob("*ignore")) == []
