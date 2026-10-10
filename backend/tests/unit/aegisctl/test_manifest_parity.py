"""The host-side manifest reader must stay the backend loader's exact twin.

``aegisctl.mounts._read_render_manifest`` duplicates
``aegis_apps.roots.manifest.MountManifest.load``, because the host-side Podman deployment
has only the ``aegisctl`` package. Drift is caught structurally, in both directions:

- every refusal the backend loader can raise is read from its source and must be produced
  by the generated corpus below, with the copy refusing identically;
- every refusal the copy can raise must be one the backend loader has.

A new check on either side therefore fails these tests until it is mirrored and covered.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from aegis_apps.roots.manifest import ManifestError, MountManifest
from aegisctl import mounts
from aegisctl.mounts import (
    MAX_INTEGER,
    MAX_MANIFEST_BYTES,
    MAX_SLOTS,
    ConfigError,
    local_identity,
    parse_config,
    preflight_slots,
    write_manifest,
)

REPOSITORY = Path(__file__).resolve().parents[4]
BACKEND_MANIFEST = REPOSITORY / "backend" / "aegis_apps" / "roots" / "manifest.py"
COPY_SOURCE = REPOSITORY / "backend" / "aegisctl" / "mounts.py"
COPY_FUNCTIONS = (
    "_read_render_manifest", "_manifest_binding", "_manifest_integer", "_private_manifest_file",
)
# Not mirrored on purpose: the renderer computes the digest that the loader compares, and
# the environment configuration belongs to running processes.
NOT_MIRRORED = frozenset({
    "invalid mount manifest digest",
    "mount manifest digest mismatch",
    "mount manifest configuration is invalid",
})
TOP_KEYS = ("version", "generatedAt", "slots")
SLOT_KEYS = (
    "slotId", "containerPath", "mode", "filesystemId", "rootInode", "expectedIdentity",
    "mountFingerprint",
)
# Every JSON type, the integer bounds, and strings shaped like each field's valid values.
VALUES: tuple[object, ...] = (
    None, True, False, 0, 1, -1, 2**32, MAX_INTEGER, MAX_INTEGER + 1, 1.5, "", "x",
    "photos", "Photos", "photos\n", "/srv/aegis/roots/photos", "/srv/aegis/roots/other",
    "read_only", "read_write", "READ_ONLY", "local:1:2", "local:0:0", "remote:nas01:/photos",
    "0" * 64, "A" * 64, "0" * 63, "2026-10-10T00:00:00Z", "2026-10-10T00:00:00",
    "2026-13-10T00:00:00Z", "x" * 40 + "Z", [], {}, [1], {"version": 1},
    # Valid ISO timestamps on both sides of the 40-character bound.
    "2026-10-10T00:00:00." + "0" * 19 + "Z", "2026-10-10T00:00:00." + "0" * 21 + "Z",
)

Outcome = tuple[str, object]
Case = tuple[str, Callable[[Path], None]]


def _messages(
    expression: ast.expr, function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Iterator[str]:
    """Regex patterns for every message an error constructor's argument can take."""
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        yield re.escape(expression.value)
    elif isinstance(expression, ast.JoinedStr):
        yield "".join(
            re.escape(part.value)
            if isinstance(part, ast.Constant) and isinstance(part.value, str) else ".+"
            for part in expression.values
        )
    elif isinstance(expression, ast.IfExp):
        yield from _messages(expression.body, function)
        yield from _messages(expression.orelse, function)
    elif isinstance(expression, ast.Name):
        assignments = [
            node.value for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == expression.id
                    for target in node.targets)
        ]
        assert assignments, f"unresolved refusal message {expression.id}"
        for value in assignments:
            yield from _messages(value, function)
    else:
        raise AssertionError(f"unrecognized refusal message: {ast.unparse(expression)}")


def _refusals(source: Path, error: str, functions: tuple[str, ...] | None = None) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    patterns: set[str] = set()
    found: set[str] = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if functions is not None and function.name not in functions:
            continue
        found.add(function.name)
        for node in ast.walk(function):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == error):
                assert len(node.args) == 1, ast.unparse(node)
                patterns.update(_messages(node.args[0], function))
    if functions is not None:
        assert found == set(functions), "a mirrored manifest function was renamed"
    return patterns


def _backend_refusals() -> set[str]:
    return {pattern for pattern in _refusals(BACKEND_MANIFEST, "ManifestError")
            if pattern not in {re.escape(message) for message in NOT_MIRRORED}}


def _base_payload(tmp_path: Path) -> dict[str, object]:
    sources = []
    for name in ("first", "second"):
        source = tmp_path / name
        source.mkdir()
        sources.append(source)
    config = tmp_path / "mounts.toml"
    config.write_text(
        "version = 1\n" + "".join(
            f'[[slots]]\nslot_id = "{slot_id}"\nsource = {json.dumps(str(source))}\n'
            f'container_path = "/srv/aegis/roots/{slot_id}"\nmode = "read_only"\n'
            f'expected_identity = "{local_identity(source)}"\n'
            for slot_id, source in zip(("photos", "uploads"), sources, strict=True)
        ),
        encoding="utf-8",
    )
    validated = tuple(
        replace(slot, mount_fingerprint=f"{index:064x}")
        for index, slot in enumerate(preflight_slots(parse_config(config)), 1)
    )
    manifest = tmp_path / "base.json"
    write_manifest(manifest, validated, uid=os.geteuid(), gid=os.getegid())
    payload = json.loads(manifest.read_bytes())
    assert isinstance(payload, dict)
    return payload


def _encoded(payload: object) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")


def _bytes_case(raw: bytes) -> Callable[[Path], None]:
    def write(path: Path) -> None:
        path.write_bytes(raw)
        path.chmod(0o600)

    return write


def _payload_case(payload: object) -> Callable[[Path], None]:
    return _bytes_case(_encoded(payload))


def _corpus(base: dict[str, object]) -> Iterator[Case]:
    def changed(change: Callable[[dict[str, object]], None]) -> dict[str, object]:
        payload = copy.deepcopy(base)
        change(payload)
        return payload

    def slot_list(payload: dict[str, object]) -> list[dict[str, object]]:
        slots = payload["slots"]
        assert isinstance(slots, list)
        return slots

    first, *rest = slot_list(base)

    def with_first(slot: object) -> dict[str, object]:
        return {**base, "slots": [slot, *rest]}

    yield "valid", _payload_case(base)
    for value in VALUES:
        yield f"payload={value!r}", _payload_case(value)
    for key in TOP_KEYS:
        yield f"missing {key}", _payload_case({k: v for k, v in base.items() if k != key})
        for value in VALUES:
            yield f"{key}={value!r}", _payload_case({**base, key: value})
    yield "extra key", _payload_case({**base, "extra": 1})
    yield "too many slots", _payload_case({**base, "slots": [first] * (MAX_SLOTS + 1)})
    distinct = [
        {**first, "slotId": f"s{index}", "containerPath": f"/srv/aegis/roots/s{index}",
         "expectedIdentity": f"remote:nas01:/s{index}", "rootInode": index + 1,
         "mountFingerprint": f"{index:064x}"}
        for index in range(MAX_SLOTS)
    ]
    yield "exactly the slot limit", _payload_case({**base, "slots": distinct})
    for length in (63, 64):
        slot_id = "a" * length
        yield f"slot ID of {length}", _payload_case({**base, "slots": [{
            **first, "slotId": slot_id, "containerPath": f"/srv/aegis/roots/{slot_id}",
        }]})
    for value in VALUES:
        yield f"slot={value!r}", _payload_case({**base, "slots": [value]})
    for key in SLOT_KEYS:
        yield f"slot missing {key}", _payload_case(
            with_first({k: v for k, v in first.items() if k != key}),
        )
        for value in VALUES:
            yield f"slot {key}={value!r}", _payload_case(with_first({**first, key: value}))
    yield "slot extra key", _payload_case(with_first({**first, "extra": 1}))

    def duplicate_slot_id(payload: dict[str, object]) -> None:
        second = slot_list(payload)[1]
        second.update(slotId="photos", containerPath="/srv/aegis/roots/photos")

    def duplicate_identity(payload: dict[str, object]) -> None:
        for slot in slot_list(payload):
            slot["expectedIdentity"] = "remote:nas01:/photos"

    def duplicate_pair(payload: dict[str, object]) -> None:
        first_slot, second = slot_list(payload)
        second.update(
            filesystemId=first_slot["filesystemId"], rootInode=first_slot["rootInode"],
            expectedIdentity="remote:nas01:/uploads",
        )

    def duplicate_fingerprint(payload: dict[str, object]) -> None:
        first_slot, second = slot_list(payload)
        second["mountFingerprint"] = first_slot["mountFingerprint"]

    for name, change in (
        ("duplicate slot ID", duplicate_slot_id),
        ("duplicate identity", duplicate_identity),
        ("duplicate pair", duplicate_pair),
        ("duplicate fingerprint", duplicate_fingerprint),
    ):
        yield name, _payload_case(changed(change))

    valid = _encoded(base)
    yield "invalid UTF-8", _bytes_case(b"\xff\xfe\xfd")
    yield "UTF-8 BOM", _bytes_case(b"\xef\xbb\xbf" + valid)
    yield "UTF-16", _bytes_case(valid.decode("ascii").encode("utf-16"))
    yield "empty", _bytes_case(b"")
    yield "truncated", _bytes_case(valid[:-10])
    yield "exactly the size limit", _bytes_case(valid.rstrip(b"\n").ljust(MAX_MANIFEST_BYTES))
    yield "oversized", _bytes_case(valid.rstrip(b"\n").ljust(MAX_MANIFEST_BYTES + 1))

    def mode(bits: int) -> Callable[[Path], None]:
        def write(path: Path) -> None:
            path.write_bytes(valid)
            path.chmod(bits)

        return write

    for bits in (0o644, 0o400, 0o700, 0o640):
        yield f"mode {bits:o}", mode(bits)

    def directory(path: Path) -> None:
        path.mkdir()

    def symlink(path: Path) -> None:
        target = path.with_name(path.name + ".target")
        target.write_bytes(valid)
        target.chmod(0o600)
        path.symlink_to(target)

    def missing(path: Path) -> None:
        del path

    yield "directory", directory
    yield "symlink", symlink
    yield "missing", missing


def _backend(path: Path) -> Outcome:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = "0" * 64
    try:
        manifest = MountManifest.load(path, digest)
    except ManifestError as exc:
        return "refused", str(exc)
    return "accepted", (manifest.digest, {
        slot_id: (slot.container_path.as_posix(), slot.mode, slot.expected_identity,
                  slot.filesystem_id, slot.root_inode, slot.mount_fingerprint)
        for slot_id, slot in manifest.slots.items()
    })


def _copy(path: Path) -> Outcome:
    try:
        digest, bindings = mounts._read_render_manifest(path)
    except ConfigError as exc:
        return "refused", str(exc)
    return "accepted", (digest, {
        slot_id: (binding.container_path, binding.mode, binding.expected_identity,
                  binding.filesystem_id, binding.root_inode, binding.mount_fingerprint)
        for slot_id, binding in bindings.items()
    })


def test_manifest_reader_matches_the_backend_loader_over_every_refusal_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _base_payload(tmp_path)
    cases = list(_corpus(base))
    mismatches: list[str] = []
    refusals: set[str] = set()
    accepted: list[str] = []
    for index, (name, prepare) in enumerate(cases):
        path = tmp_path / f"case-{index}.json"
        prepare(path)
        outcomes = (_backend(path), _copy(path))
        if outcomes[0] != outcomes[1]:
            mismatches.append(f"{name}: backend {outcomes[0]!r}, copy {outcomes[1]!r}")
        if outcomes[0][0] == "refused":
            refusals.add(str(outcomes[0][1]))
        else:
            accepted.append(name)
    # The owner check: both loaders compare against the invoking IDs.
    owner = tmp_path / "owner.json"
    _payload_case(base)(owner)
    with monkeypatch.context() as patched:
        patched.setattr(os, "getegid", lambda: os.getgid() + 1)
        outcomes = (_backend(owner), _copy(owner))
    if outcomes[0] != outcomes[1]:
        mismatches.append(f"owner: backend {outcomes[0]!r}, copy {outcomes[1]!r}")
    refusals.add(str(outcomes[0][1]))

    assert not mismatches, "\n".join(mismatches)
    assert {
        "valid", "UTF-8 BOM", "UTF-16", "exactly the size limit", "exactly the slot limit",
        "slot ID of 63", f"generatedAt={'2026-10-10T00:00:00.' + '0' * 19 + 'Z'!r}",
    } <= set(accepted)
    uncovered = sorted(
        pattern for pattern in _backend_refusals()
        if not any(re.fullmatch(pattern, message) for message in refusals)
    )
    assert not uncovered, (
        "the backend manifest loader has refusals that this corpus never produces; extend "
        "the corpus and mirror each check in aegisctl.mounts._read_render_manifest: "
        f"{uncovered}"
    )


def test_manifest_reader_refuses_nothing_the_backend_loader_lacks() -> None:
    copied = _refusals(COPY_SOURCE, "ConfigError", COPY_FUNCTIONS)
    backend = _backend_refusals()

    assert copied, "the mirrored manifest functions raise no refusals"
    assert copied <= backend, sorted(copied - backend)
