"""Quadlet templates and the installer's pure renderer (spec D1, D2, D5, D6)."""
from __future__ import annotations

import ast
import dataclasses
import ipaddress
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support.podman_deploy import (
    INSTALLER,
    SAMPLE_BACKEND_SUBNET,
    SAMPLE_DERIVATIVES_DIR,
    SAMPLE_HOST_GID,
    SAMPLE_IMAGES,
    SAMPLE_INSTALL_TOKEN,
    SAMPLE_LABEL_LEVEL,
    SAMPLE_PORT,
    SAMPLE_PREFIX,
    SAMPLE_READER_TYPE,
    SAMPLE_RELEASE,
    SAMPLE_SECRETS_DIR,
    Unit,
    environment_assignments,
    load_installer,
    network_line,
    parse_unit,
    sample_config,
    systemd_words,
)

NETWORKS = ("backend", "edge", "tls-hop")
VOLUMES = (
    "postgres-data", "indexer-coordination", "staging", "derivatives", "quarantine", "caddy-data",
    "media-scratch",
)
CONTAINERS = (
    "postgres", "migrate", "web", "operations", "indexer", "media", "media-processor", "gateway",
    "caddy",
)
TEMPLATES = (
    *(f"{name}.network" for name in NETWORKS),
    *(f"{name}.volume" for name in VOLUMES),
    *(f"{name}.container" for name in CONTAINERS),
    "caddy.socket",
)
BACKEND = ("migrate", "web", "operations", "indexer", "media")
LONG_RUNNING = (
    "postgres", "web", "operations", "indexer", "media", "media-processor", "gateway", "caddy",
)
# Units whose systemd start completes only once the container's health check passes.
HEALTH_GATED = ("postgres", "web", "operations", "indexer", "media")
# D5: the containers that mount originals, plus every other backend-image service: web
# shares staging with operations, and all of them read the mount manifest, which must look
# owned by 10001:10001 inside each.
GROUP_MAPPED = {"gateway": 101, "operations": 10001, "indexer": 10001, "media": 10001,
                "media-processor": 10001, "web": 10001, "migrate": 10001}
# D6: the containers that mount originals run as the dedicated reader type.
READER_LABELED = {"gateway", "operations", "indexer", "media", "media-processor"}
IMAGE_ROLE = {
    "postgres": "postgres", "gateway": "gateway", "caddy": "caddy", "media-processor": "media",
} | dict.fromkeys(BACKEND, "backend")
PUBLIC_URL = f"https://aegis.example.test:{SAMPLE_PORT}"
# Compose's file secrets: each is a read-only bind of <secrets dir>/<name> at its target.
SECRET_BINDS = {
    "postgres": {
        name: f"/run/aegis-source-secrets/{name.replace('-', '_')}"
        for name in (
            "postgres-superuser-password", "db-migrator-password", "db-web-password",
            "db-operations-password", "db-indexer-password", "db-media-password",
        )
    },
    "migrate": {"django-secret-key": "/run/secrets/django_secret_key",
                "db-migrator-password": "/run/secrets/db_migrator_password"},
    "web": {"django-secret-key": "/run/secrets/django_secret_key",
            "db-web-password": "/run/secrets/db_web_password",
            "auth-throttle-hmac-key": "/run/secrets/auth_throttle_hmac_key"},
} | {
    role: {"django-secret-key": "/run/secrets/django_secret_key",
           f"db-{role}-password": f"/run/secrets/db_{role}_password"}
    for role in ("operations", "indexer", "media")
} | {"gateway": {}, "caddy": {}, "media-processor": {}}
# Fixed backend addresses: nginx pins web and web pins the gateway when each starts.
STATIC_BACKEND_HOSTS = {"web": 10, "gateway": 11}
IMAGE_ID = "e" * 64


def rendered(**overrides: Any) -> dict[str, str]:
    return dict(load_installer().render_units(sample_config(**overrides)))


def units(**overrides: Any) -> dict[str, Unit]:
    return {name: parse_unit(text) for name, text in rendered(**overrides).items()}


def container(name: str, **overrides: Any) -> Unit:
    return units(**overrides)[f"{SAMPLE_PREFIX}{name}.container"]


def real_templates() -> dict[str, str]:
    return dict(load_installer().load_templates())


def test_rendering_yields_exactly_the_expected_unit_files() -> None:
    assert set(rendered()) == {f"{SAMPLE_PREFIX}{name}" for name in TEMPLATES}
    assert set(real_templates()) == set(TEMPLATES)


def object_references(unit: Unit) -> list[str]:
    """Every Podman object or unit name a rendered unit names."""
    names = [
        value
        for section, key in (
            ("Container", "ContainerName"), ("Network", "NetworkName"), ("Volume", "VolumeName"),
        )
        for value in unit.values(section, key)
    ]
    # Network=none names no object: the processor has no network at all.
    names += [value.partition(":")[0] for value in unit.values("Container", "Network")
              if value != "none"]
    names += [
        value.partition(":")[0] for value in unit.values("Container", "Volume")
        if not value.startswith("/")
    ]
    names += [
        word
        for key in ("After", "Before", "Requires", "Wants", "BindsTo", "PartOf")
        for value in unit.values("Unit", key) for word in value.split()
    ]
    return names


@pytest.mark.parametrize("prefix", [SAMPLE_PREFIX, "aegis-test-0f3a-"])
def test_every_file_and_object_reference_carries_the_prefix(prefix: str) -> None:
    texts = rendered(prefix=prefix)

    assert set(texts) == {f"{prefix}{name}" for name in TEMPLATES}
    for name, text in texts.items():
        references = object_references(parse_unit(text))
        assert all(reference.startswith(prefix) for reference in references), name
        if name.endswith(".container"):
            assert references, name


def test_rendered_units_keep_no_token_and_carry_the_values() -> None:
    texts = rendered()
    joined = "\n".join(texts.values())

    assert "@@" not in joined
    for value in (SAMPLE_RELEASE, PUBLIC_URL, SAMPLE_READER_TYPE, *SAMPLE_IMAGES.values()):
        assert value in joined
    assert f"+g10001:@{SAMPLE_HOST_GID}:1" in joined


@pytest.mark.parametrize("token", [
    "@@NOT_A_TOKEN@@", "@@lower@@", "@@ PREFIX@@", "@@PREFIX", "PREFIX@@", "@@@@",
])
def test_unknown_or_malformed_tokens_are_refused(token: str) -> None:
    installer = load_installer()
    templates = real_templates()
    templates["web.container"] += f"\n# {token}\n"

    with pytest.raises(installer.RenderError, match="token"):
        installer.render_units(sample_config(), templates)


def test_the_template_set_must_be_exact() -> None:
    installer = load_installer()
    missing = real_templates()
    del missing["caddy.socket"]
    extra = real_templates() | {"other.container": "[Container]\nImage=x\n"}

    for templates in (missing, extra):
        with pytest.raises(installer.RenderError, match="template set"):
            installer.render_units(sample_config(), templates)


def test_the_template_directory_must_hold_exactly_the_regular_templates(tmp_path: Path) -> None:
    installer = load_installer()
    for name, text in real_templates().items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    assert installer.load_templates(tmp_path) == real_templates()

    (tmp_path / "caddy.socket").unlink()
    (tmp_path / "caddy.socket").symlink_to(tmp_path / "web.container")
    with pytest.raises(installer.RenderError, match="not a regular file"):
        installer.load_templates(tmp_path)
    (tmp_path / "caddy.socket").unlink()
    with pytest.raises(installer.RenderError, match="template set"):
        installer.load_templates(tmp_path)
    with pytest.raises(installer.RenderError, match="cannot read"):
        installer.load_templates(tmp_path / "missing")


INVALID_VALUES = [
    ("prefix", ""), ("prefix", "aegis"), ("prefix", "Aegis-"), ("prefix", "1aegis-"),
    ("prefix", "-aegis-"), ("prefix", "aegis_-"), ("prefix", "a" * 32 + "-"),
    ("https_port", 0), ("https_port", 443), ("https_port", 1023), ("https_port", 65536),
    ("https_port", True), ("https_port", "9443"),
    ("release", "sha-" + "0" * 39), ("release", "sha-" + "0" * 41), ("release", "sha-" + "A" * 40),
    ("release", "sha-" + "g" * 40), ("release", "0" * 40), ("release", "latest"),
    ("release", "test-run"),
    ("image_backend", "registry.example.test/aegis-backend:sha-" + "0" * 40),
    ("image_backend", "registry.example.test/aegis-backend@sha256:" + "a" * 63),
    ("image_backend", "registry.example.test/aegis-backend@sha256:" + "A" * 64),
    ("image_backend", "registry.example.test/aegis-backend:tag@sha256:" + "a" * 64),
    ("image_backend", "Registry.example.test/aegis-backend@sha256:" + "a" * 64),
    ("image_backend", "registry.example.test/aegis backend@sha256:" + "a" * 64),
    ("image_backend", "registry.example.test:99999/aegis-backend@sha256:" + "a" * 64),
    ("image_backend", "registry.example.test:65536/aegis-backend@sha256:" + "a" * 64),
    ("image_backend", "registry.example.test:0/aegis-backend@sha256:" + "a" * 64),
    ("image_gateway", IMAGE_ID), ("image_postgres", "sha256:" + "a" * 64), ("image_caddy", ""),
    ("image_media", IMAGE_ID), ("image_media", "registry.example.test/aegis-media:sha-" + "0" * 40),
    ("image_media", None),
    ("host_gid", 0), ("host_gid", -1), ("host_gid", True), ("host_gid", "1000"),
    ("host_gid", 2**32 - 1),
    ("reader_type", ""), ("reader_type", "container"), ("reader_type", "spc_t"),
    ("reader_type", "unconfined_t"), ("reader_type", "Container_t"),
    ("reader_type", "aegis reader_t"), ("reader_type", "aegis_reader.process.x"),
    ("reader_type", "kernel_t"), ("reader_type", "container_runtime_t"),
    ("reader_type", "unconfined_service_t"), ("reader_type", "aegis_.process"),
    ("reader_type", "Aegis_reader.process"), ("reader_type", "aegis_reader_t"),
    ("reader_type", "other_reader.process"),
    ("names", ()), ("names", "aegis.example.test"), ("names", ["aegis.example.test"]),
    ("names", ("*.example.test",)), ("names", ("aegis.example.test,other.example.test",)),
    ("names", ("bad_name.example.test",)), ("names", ("-aegis.example.test",)),
    ("names", ("aegis-.example.test",)), ("names", ("a" * 64 + ".example.test",)),
    ("names", ("aegis.example.123",)), ("names", ("999.1.1.1",)), ("names", ("fe80::1%1",)),
    ("names", ("\u212aegis.example.test",)), ("names", ("::ffff:192.0.2.1",)),
    ("names", ("0.0.0.0",)), ("names", ("::",)), ("names", ("aegis.example.test.",)),
    ("names", ("",)), ("names", ("aegis.example.test:9443",)),
    ("names", ("https://aegis.example.test",)), ("names", ("aegis..example.test",)),
    ("names", (".".join(["a" * 63] * 4),)),
    ("names", ("aegis.example.test", "AEGIS.example.test")),
    ("allow_test_values", 1),
    ("secrets_dir", ""), ("secrets_dir", "relative/secrets"), ("secrets_dir", "/"),
    ("secrets_dir", "/srv/aegis/../secrets"), ("secrets_dir", "/srv//secrets"),
    ("secrets_dir", "/srv/secrets/"), ("secrets_dir", "/srv/./secrets"),
    ("secrets_dir", "/srv/aegis:secrets"), ("secrets_dir", "/srv/aegis,secrets"),
    ("secrets_dir", "/srv/aegis secrets"), ("secrets_dir", "/srv/aegis\tsecrets"),
    ("secrets_dir", "/srv/aegis%nsecrets"), ("secrets_dir", "/srv/$HOME/secrets"),
    ("secrets_dir", 7), ("secrets_dir", "/srv/a\x01b"), ("secrets_dir", "/srv/a\x7fb"),
    ("secrets_dir", "/srv/`x`"), ("secrets_dir", "/srv/a;b#c"), ("secrets_dir", "/srv/{x}"),
    ("secrets_dir", "/srv/\u00e4"), ("secrets_dir", "/srv/a*b"), ("secrets_dir", "/srv/a?b"),
    ("backend_subnet", ""), ("backend_subnet", "10.123.45.0"), ("backend_subnet", "10.123.0.0/16"),
    ("backend_subnet", "10.123.45.0/25"), ("backend_subnet", "10.123.45.1/24"),
    ("backend_subnet", "10.123.45.0/255.255.255.0"), ("backend_subnet", "8.8.8.0/24"),
    ("backend_subnet", "100.64.0.0/24"), ("backend_subnet", "172.32.0.0/24"),
    ("backend_subnet", "192.169.0.0/24"), ("backend_subnet", "fd00:1::/64"),
    ("backend_subnet", "010.123.45.0/24"), ("backend_subnet", 24),
    ("install_token", ""), ("install_token", "0" * 31), ("install_token", "0" * 33),
    ("install_token", "A" * 32), ("install_token", "g" * 32), ("install_token", 7),
    # The shared MCS level: s0, two categories, the lower first, at most c1023.
    ("label_level", ""), ("label_level", "s0"), ("label_level", "s0:c5"),
    ("label_level", "s0:c5,c5"), ("label_level", "s0:c9,c5"), ("label_level", "s0:c05,c9"),
    ("label_level", "s0:c5,c1024"), ("label_level", "s0:c5,c10000"), ("label_level", "s1:c5,c9"),
    ("label_level", "s0-s0:c5,c9"), ("label_level", "s0:c5.c9"), ("label_level", "s0:c5,c9,c11"),
    ("label_level", "s0:c5,c9 "), ("label_level", 7), ("label_level", None),
    # The derivatives directory is bound by Device=: literal, absolute and normalized.
    ("derivatives_dir", ""), ("derivatives_dir", "relative/derivatives"), ("derivatives_dir", "/"),
    ("derivatives_dir", "/srv/../derivatives"), ("derivatives_dir", "/srv//derivatives"),
    ("derivatives_dir", "/srv/derivatives/"), ("derivatives_dir", "/srv/a:b"),
    ("derivatives_dir", "/srv/a,b"), ("derivatives_dir", "/srv/a b"),
    ("derivatives_dir", "/srv/%h/derivatives"), ("derivatives_dir", "/srv/$HOME"),
    ("derivatives_dir", "/srv/\u00e4"), ("derivatives_dir", 7), ("derivatives_dir", b"/srv/d"),
]


@pytest.mark.parametrize(("field", "value"), INVALID_VALUES)
def test_each_invalid_value_class_is_refused(field: str, value: object) -> None:
    installer = load_installer()

    with pytest.raises(installer.RenderError):
        installer.render_units(sample_config(**{field: value}))


@pytest.mark.parametrize("field", [
    "prefix", "release", "reader_type", "image_backend", "image_gateway", "image_postgres",
    "image_caddy", "image_media", "names", "secrets_dir", "backend_subnet", "install_token",
    "derivatives_dir", "label_level",
])
@pytest.mark.parametrize("hazard", ["\n", "\r", "\0", "@@"])
def test_values_with_line_breaks_nul_or_token_markers_are_refused(
    field: str, hazard: str,
) -> None:
    installer = load_installer()
    current = getattr(sample_config(derivatives_dir=SAMPLE_DERIVATIVES_DIR), field)
    value: object = (
        (current[0] + hazard, *current[1:]) if isinstance(current, tuple) else current + hazard
    )

    with pytest.raises(installer.RenderError):
        installer.render_units(sample_config(**{field: value}))


@pytest.mark.parametrize(("field", "value"), [
    ("label_level", "s0:c0,c1"), ("label_level", "s0:c0,c1023"),
    ("label_level", "s0:c1022,c1023"),
    ("reader_type", "container_t"), ("reader_type", "aegis_reader2.process"),
    ("secrets_dir", "/srv/aegis_1.2@x+y-z/Secrets"),
    ("image_backend", "registry.example.test:65535/aegis-backend@sha256:" + "a" * 64),
    ("image_backend", "registry.example.test:5000/aegis-backend@sha256:" + "a" * 64),
])
def test_boundary_values_that_stay_valid_render(field: str, value: object) -> None:
    assert rendered(**{field: value})


def test_test_release_and_local_image_ids_need_the_explicit_test_flag() -> None:
    installer = load_installer()
    test_values = {"release": "test-0f3a", "image_backend": IMAGE_ID}
    for field, value in test_values.items():
        with pytest.raises(installer.RenderError):
            installer.render_units(sample_config(**{field: value}))

    texts = rendered(**test_values, allow_test_values=True)
    web = parse_unit(texts[f"{SAMPLE_PREFIX}web.container"])
    assert web.value("Container", "Image") == IMAGE_ID
    assert environment_assignments(web)["AEGIS_RELEASE_ID"] == "test-0f3a"


def test_no_unit_publishes_a_port_or_passes_raw_podman_arguments() -> None:
    for name, text in rendered().items():
        unit = parse_unit(text)
        assert "PublishPort" not in unit.keys("Container"), name
        assert "ExposeHostPort" not in unit.keys("Container"), name
        # Raw arguments would bypass every check here, a restart policy included.
        assert "PodmanArgs" not in unit.keys("Container"), name
        assert "--publish" not in text and "--restart" not in text, name


def network_aliases(unit: Unit) -> dict[str, list[str]]:
    joined: dict[str, list[str]] = {}
    for line in unit.values("Container", "Network"):
        reference, aliases, _ = network_line(line)
        assert reference.startswith(SAMPLE_PREFIX) and reference.endswith(".network"), line
        joined[reference.removeprefix(SAMPLE_PREFIX).removesuffix(".network")] = aliases
    return joined


def test_each_container_joins_its_networks_under_its_compose_service_name() -> None:
    rendered_units = units()
    expected_networks = dict.fromkeys(
        ("postgres", "migrate", "web", "operations", "indexer", "media"), ("backend",),
    ) | {"gateway": NETWORKS, "caddy": ("edge", "tls-hop")}

    for name, networks in expected_networks.items():
        joined = network_aliases(rendered_units[f"{SAMPLE_PREFIX}{name}.container"])
        assert tuple(joined) == networks, name
        for network, aliases in joined.items():
            assert aliases[0] == name, (name, network)
    # Spec M2: the processor joins no network at all, as compose's network_mode: none.
    processor = rendered_units[f"{SAMPLE_PREFIX}media-processor.container"]
    assert processor.values("Container", "Network") == ["none"]
    assert set(expected_networks) | {"media-processor"} == set(CONTAINERS)


def test_tls_gateway_alias_exists_only_on_the_gateway_tls_hop_line() -> None:
    lines = [
        (name, line)
        for name, unit in units().items()
        for line in unit.values("Container", "Network")
        if "tls-gateway" in line
    ]

    assert lines == [(
        f"{SAMPLE_PREFIX}gateway.container",
        f"{SAMPLE_PREFIX}tls-hop.network:alias=gateway,alias=tls-gateway",
    )]
    assert sum("tls-gateway" in text for text in rendered().values()) == 1


def test_group_mapping_and_reader_label_appear_exactly_on_the_d5_and_d6_sets() -> None:
    rendered_units = units()
    group_maps = {
        name.removeprefix(SAMPLE_PREFIX).removesuffix(".container"): unit.values(
            "Container", "GIDMap",
        )
        for name, unit in rendered_units.items() if unit.values("Container", "GIDMap")
    }
    labels = {
        name.removeprefix(SAMPLE_PREFIX).removesuffix(".container"): unit.values(
            "Container", "SecurityLabelType",
        )
        for name, unit in rendered_units.items() if unit.values("Container", "SecurityLabelType")
    }

    assert group_maps == {
        name: [f"+g{gid}:@{SAMPLE_HOST_GID}:1"] for name, gid in GROUP_MAPPED.items()
    }
    assert labels == {name: [SAMPLE_READER_TYPE] for name in READER_LABELED}
    # Each mapping names the container's own primary group.
    for name, gid in GROUP_MAPPED.items():
        group = rendered_units[f"{SAMPLE_PREFIX}{name}.container"].value("Container", "Group")
        assert group == str(gid), name


def test_networks_and_volumes_carry_prefixed_names_and_internal_flags() -> None:
    rendered_units = units()
    for name in NETWORKS:
        network = rendered_units[f"{SAMPLE_PREFIX}{name}.network"]
        assert network.value("Network", "NetworkName") == f"{SAMPLE_PREFIX}{name}"
        assert network.value("Network", "Internal") == (None if name == "edge" else "true")
    for name in VOLUMES:
        volume = rendered_units[f"{SAMPLE_PREFIX}{name}.volume"]
        assert volume.value("Volume", "VolumeName") == f"{SAMPLE_PREFIX}{name}"


def test_networks_and_volumes_carry_the_install_token_and_nothing_else_does() -> None:
    # --purge-data removes a volume or network only when it carries this install's token.
    label = f"io.aegis.install={SAMPLE_INSTALL_TOKEN}"
    for name, unit in units().items():
        section = {".network": "Network", ".volume": "Volume"}.get(Path(name).suffix)
        if section is None:
            assert all("Label" not in unit.keys(part) for part in unit.sections), name
        else:
            assert unit.values(section, "Label") == [label], name
    volume = units()[f"{SAMPLE_PREFIX}staging.volume"]
    assert volume.sections == {"Volume": [
        ("VolumeName", f"{SAMPLE_PREFIX}staging"), ("Label", label),
    ]}


def test_every_container_is_named_for_its_service_and_runs_its_release_image() -> None:
    for name in CONTAINERS:
        unit = container(name)
        assert unit.value("Container", "ContainerName") == f"{SAMPLE_PREFIX}{name}"
        assert unit.value("Container", "Image") == SAMPLE_IMAGES[IMAGE_ROLE[name]], name
        for volume in unit.values("Container", "Volume"):
            assert re.fullmatch(
                rf"{SAMPLE_PREFIX}[a-z-]+\.volume:/[a-z/-]+(:ro)?"
                rf"|{re.escape(SAMPLE_SECRETS_DIR)}/[a-z-]+:/[a-z/_-]+:ro",
                volume,
            ), name


def test_startup_order_follows_compose_conditions_and_web_finds_the_gateway() -> None:
    rendered_units = units()

    def unit(name: str) -> Unit:
        return rendered_units[f"{SAMPLE_PREFIX}{name}.container"]

    def references(name: str, key: str) -> set[str]:
        return {
            word.removeprefix(SAMPLE_PREFIX).removesuffix(".container")
            for value in unit(name).values("Unit", key) for word in value.split()
        }

    migrate = unit("migrate")
    assert migrate.value("Service", "Type") == "oneshot"
    assert migrate.value("Service", "RemainAfterExit") == "yes"
    assert references("migrate", "Requires") == references("migrate", "After") == {"postgres"}
    for name in ("web", "operations", "indexer", "media"):
        assert references(name, "Requires") == {"migrate"}, name
    for name in ("operations", "indexer", "media"):
        assert references(name, "After") == {"migrate"}, name
    # web resolves the gateway's address at start, so the gateway container must run first;
    # the gateway in turn becomes healthy only once web answers it.
    assert references("web", "After") == {"migrate", "gateway"}
    assert references("web", "Wants") == {"gateway"}
    assert references("gateway", "After") == {"migrate"}
    assert references("gateway", "Requires") == set()
    assert references("caddy", "After") == {"caddy.socket", "gateway", "web"}
    assert references("caddy", "Requires") == {"caddy.socket"}
    assert references("postgres", "After") == set()
    # The processor needs neither the database nor the worker: either one creates the
    # scratch directories, and Quadlet orders it after its scratch volume by itself.
    for key in ("After", "Requires", "Wants"):
        assert references("media-processor", key) == set(), key
    for name in CONTAINERS:
        notify = unit(name).value("Container", "Notify")
        assert notify == ("healthy" if name in HEALTH_GATED else None), name


def test_long_running_services_retry_without_limit_and_start_at_boot() -> None:
    # Like compose's unless-stopped: unlimited restarts, with backoff from 5s to Docker's 1min cap.
    for name in LONG_RUNNING:
        unit = container(name)
        assert unit.sections["Service"][:4] == [
            ("Restart", "on-failure"), ("RestartSec", "5"), ("RestartSteps", "8"),
            ("RestartMaxDelaySec", "60"),
        ], name
        # A failed PostgreSQL attempt must not fail the queued migrate and backend starts.
        expected_mode = "direct" if name == "postgres" else None
        assert unit.value("Service", "RestartMode") == expected_mode, name
        assert unit.value("Unit", "StartLimitIntervalSec") == "0", name
        assert unit.value("Unit", "StartLimitBurst") is None, name
        assert unit.values("Install", "WantedBy") == ["default.target"], name


def test_postgres_start_timeout_allows_crash_recovery() -> None:
    assert container("postgres").value("Service", "TimeoutStartSec") == "900"


def test_migrate_retries_in_place_so_dependent_starts_wait_for_its_success() -> None:
    migrate = container("migrate")

    assert migrate.sections["Service"] == [
        ("Type", "oneshot"), ("RemainAfterExit", "yes"), ("Restart", "on-failure"),
        ("RestartMode", "direct"), ("RestartSec", "5"), ("RestartSteps", "6"),
        ("RestartMaxDelaySec", "60"),
    ]
    assert migrate.value("Unit", "StartLimitIntervalSec") == "0"
    assert migrate.value("Unit", "StartLimitBurst") is None
    assert "Install" not in migrate.sections


def test_no_container_start_reaches_a_registry() -> None:
    for name in CONTAINERS:
        assert container(name).value("Container", "Pull") == "never", name


def test_postgres_readiness_probes_tcp_not_the_reconcile_socket() -> None:
    assert container("postgres").value("Container", "HealthCmd") == (
        'pg_isready -h 127.0.0.1 -p 5432 -U postgres -d "aegis"'
    )
    assert environment_assignments(container("postgres"))["POSTGRES_DB"] == "aegis"


def test_install_sections_can_be_left_out_for_units_that_must_not_start_at_boot() -> None:
    installed = rendered()
    uninstalled = rendered(install_sections=False)

    assert set(uninstalled) == set(installed)
    with_install = {name for name, text in installed.items() if "[Install]" in text}
    assert with_install == {
        f"{SAMPLE_PREFIX}{name}.container" for name in LONG_RUNNING
    } | {f"{SAMPLE_PREFIX}caddy.socket"}
    for name, text in uninstalled.items():
        unit, full = parse_unit(text), parse_unit(installed[name])
        assert "Install" not in unit.sections and "[Install]" not in text, name
        assert unit.sections == {
            section: keys for section, keys in full.sections.items() if section != "Install"
        }, name
    with pytest.raises(load_installer().RenderError):
        rendered(install_sections=1)


def test_health_gated_starts_outlast_the_compose_health_window() -> None:
    def seconds(value: str | None) -> int:
        assert value is not None and value.endswith("s")
        return int(value[:-1])

    for name in HEALTH_GATED:
        unit = container(name)
        assert unit.value("Container", "HealthCmd"), name
        window = seconds(unit.value("Container", "HealthStartPeriod") or "0s") + int(
            unit.value("Container", "HealthRetries") or "0"
        ) * (
            seconds(unit.value("Container", "HealthInterval"))
            + seconds(unit.value("Container", "HealthTimeout"))
        )
        timeout = unit.value("Service", "TimeoutStartSec")
        assert timeout is not None and int(timeout) >= window, name


def test_production_environment_replaces_development_defaults() -> None:
    for name in BACKEND:
        environment = environment_assignments(container(name))
        assert environment["AEGIS_ENV"] == "production", name
        assert environment["DJANGO_SETTINGS_MODULE"] == "aegis.settings.production", name
        assert environment["AEGIS_RELEASE_ID"] == SAMPLE_RELEASE, name
        assert environment["AEGIS_PUBLIC_URL"] == PUBLIC_URL, name
        assert environment["AEGIS_ALLOWED_HOSTS"] == "aegis.example.test,192.0.2.10", name
    assert environment_assignments(container("gateway"))["AEGIS_PUBLIC_URL"] == PUBLIC_URL
    assert environment_assignments(container("caddy")) == {
        "AEGIS_LAN_SITES": f"{PUBLIC_URL}, https://192.0.2.10:{SAMPLE_PORT}",
    }


def test_names_are_lowercased_and_ipv6_literals_are_bracketed() -> None:
    names = ("Aegis.Example.Test", "2001:DB8::10", "192.0.2.10")
    web = environment_assignments(container("web", names=names))
    caddy = environment_assignments(container("caddy", names=names))

    assert web["AEGIS_PUBLIC_URL"] == PUBLIC_URL
    assert web["AEGIS_ALLOWED_HOSTS"] == "aegis.example.test,[2001:db8::10],192.0.2.10"
    assert caddy["AEGIS_LAN_SITES"] == ", ".join(
        f"https://{host}:{SAMPLE_PORT}"
        for host in ("aegis.example.test", "[2001:db8::10]", "192.0.2.10")
    )


def test_caddy_serves_the_lan_configuration_on_the_systemd_socket() -> None:
    caddy = container("caddy")
    socket = units()[f"{SAMPLE_PREFIX}caddy.socket"]

    assert systemd_words(caddy.value("Container", "Exec") or "") == [
        "caddy", "run", "--config", "/etc/caddy/Caddyfile.lan", "--adapter", "caddyfile",
    ]
    assert caddy.values("Container", "Volume") == [f"{SAMPLE_PREFIX}caddy-data.volume:/data"]
    assert caddy.values("Container", "Tmpfs") == [
        "/config:size=16m,noexec,nosuid,nodev,U,notmpcopyup,mode=0700",
        "/tmp:size=16m,noexec,nosuid,nodev,U,notmpcopyup,mode=1777",
    ]
    assert set(socket.sections) == {"Unit", "Socket", "Install"}
    assert socket.sections["Socket"] == [("ListenStream", str(SAMPLE_PORT))]
    assert socket.values("Install", "WantedBy") == ["sockets.target"]


def test_secrets_are_read_only_binds_of_installer_files_never_podman_secrets() -> None:
    # Podman mounts its secrets read-write, and the PostgreSQL entrypoint refuses any source
    # secret that is not a read-only bind, as compose's file secrets are.
    for name, text in rendered().items():
        assert "Secret" not in parse_unit(text).keys("Container"), name
        assert "--secret" not in text, name
    for name in CONTAINERS:
        binds = {}
        for volume in container(name).values("Container", "Volume"):
            if not volume.startswith("/"):
                continue
            source, target, options = volume.split(":")
            directory, _, file_name = source.rpartition("/")
            assert directory == SAMPLE_SECRETS_DIR and options == "ro", volume
            binds[file_name] = target
        assert binds == SECRET_BINDS[name], name


def test_only_web_and_the_gateway_get_fixed_backend_addresses_outside_the_pool() -> None:
    subnet = ipaddress.ip_network(SAMPLE_BACKEND_SUBNET)
    pool = ipaddress.ip_network(f"{subnet[128]}/25")
    backend = units()[f"{SAMPLE_PREFIX}backend.network"]
    assert backend.value("Network", "Subnet") == SAMPLE_BACKEND_SUBNET
    assert backend.value("Network", "IPRange") == str(pool)
    for name in ("edge", "tls-hop"):
        network = units()[f"{SAMPLE_PREFIX}{name}.network"]
        assert {"Subnet", "IPRange"}.isdisjoint(network.keys("Network")), name

    addresses = {}
    for name in CONTAINERS:
        for line in container(name).values("Container", "Network"):
            reference, aliases, options = network_line(line)
            assert set(options) <= {"ip"}, line
            if "ip" in options:
                assert reference == f"{SAMPLE_PREFIX}backend.network", line
                assert aliases[0] == name, line
                addresses[name] = ipaddress.ip_address(options["ip"])
    assert addresses == {
        name: subnet[host] for name, host in STATIC_BACKEND_HOSTS.items()
    }
    for address in addresses.values():
        assert address in subnet and address not in pool


def test_backend_subnets_in_each_private_range_derive_their_addresses() -> None:
    for subnet in ("10.0.0.0/24", "172.31.255.0/24", "192.168.200.0/24"):
        texts = rendered(backend_subnet=subnet)
        prefix = subnet.rpartition(".")[0]
        backend = parse_unit(texts[f"{SAMPLE_PREFIX}backend.network"])
        web = parse_unit(texts[f"{SAMPLE_PREFIX}web.container"])
        assert backend.value("Network", "IPRange") == f"{prefix}.128/25"
        assert f"{SAMPLE_PREFIX}backend.network:alias=web,ip={prefix}.10" in web.values(
            "Container", "Network",
        )


def test_each_rendered_unit_names_its_install_directory() -> None:
    installer = load_installer()
    texts = rendered()

    # Quadlet reads only its own unit types; the socket is a plain systemd user unit.
    assert {
        name: installer.unit_directory(name) for name in texts
    } == {
        name: "systemd/user" if name.endswith(".socket") else "containers/systemd"
        for name in texts
    }
    with pytest.raises(installer.RenderError):
        installer.unit_directory("aegis-web.service")


def test_installer_is_a_python_311_standard_library_executable() -> None:
    """Static only: 3.11 syntax and standard-library imports.

    Nothing here runs the installer on Python 3.11, so a standard-library API added after
    3.11 would not be caught; keep to APIs that 3.11 documents.
    """
    source = INSTALLER.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 11))
    imported = {
        alias.name.partition(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {
        (node.module or "").partition(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level == 0
    }

    assert source.startswith("#!/usr/bin/env python3\n")
    assert INSTALLER.stat().st_mode & stat.S_IXUSR
    assert os.access(INSTALLER, os.X_OK)
    assert imported <= sys.stdlib_module_names | {"__future__"}


# -- the media processor and the derivatives volume (spec M2, M8) -------------------------------


def test_the_media_processor_is_offline_credential_free_read_only_and_bounded() -> None:
    processor = container("media-processor")

    assert processor.value("Container", "Image") == SAMPLE_IMAGES["media"]
    assert processor.values("Container", "Network") == ["none"]
    # No secret bind, no database setting, no environment at all.
    assert processor.values("Container", "Environment") == []
    assert processor.values("Container", "Volume") == [
        f"{SAMPLE_PREFIX}media-scratch.volume:/srv/aegis/scratch",
    ]
    assert processor.value("Container", "Exec") is None  # the image's own processor loop
    assert (processor.value("Container", "User"), processor.value("Container", "Group")) == (
        "10001", "10001",
    )
    assert processor.value("Container", "ReadOnly") == "true"
    assert processor.values("Container", "Tmpfs") == ["/tmp:size=64m,noexec,nosuid,nodev"]
    assert processor.value("Container", "DropCapability") == "ALL"
    assert processor.value("Container", "AddCapability") is None
    assert processor.value("Container", "NoNewPrivileges") == "true"
    assert processor.value("Container", "RunInit") == "true"
    assert processor.value("Container", "Unmask") == "/sys/devices/virtual/powercap"
    assert processor.value("Container", "PidsLimit") == "256"
    assert processor.value("Container", "Memory") == "13g"
    assert processor.value("Service", "CPUQuota") == "400%"
    assert processor.value("Container", "HealthCmd") is None
    assert processor.value("Container", "Notify") is None


def test_only_the_worker_and_the_processor_share_scratch_and_only_the_gateway_reads_derivatives(
) -> None:
    mounts: dict[str, dict[str, str]] = {}
    for name in CONTAINERS:
        for line in container(name).values("Container", "Volume"):
            source, target, *options = line.split(":")
            if source.startswith(SAMPLE_PREFIX):
                volume = source.removeprefix(SAMPLE_PREFIX).removesuffix(".volume")
                mounts.setdefault(volume, {})[name] = ":".join([target, *options])

    assert mounts["media-scratch"] == {
        "media": "/srv/aegis/scratch", "media-processor": "/srv/aegis/scratch",
    }
    assert mounts["derivatives"] == {
        "media": "/srv/aegis/derivatives", "gateway": "/srv/aegis/derivatives:ro",
    }
    assert mounts["quarantine"] == {"media": "/srv/aegis/quarantine"}


def test_the_derivatives_volume_binds_the_operator_directory_only_when_configured() -> None:
    plain = units()
    bound = units(derivatives_dir=SAMPLE_DERIVATIVES_DIR)
    name = f"{SAMPLE_PREFIX}derivatives.volume"
    label = ("Label", f"io.aegis.install={SAMPLE_INSTALL_TOKEN}")

    assert plain[name].sections == {"Volume": [
        ("VolumeName", f"{SAMPLE_PREFIX}derivatives"), label,
    ]}
    assert bound[name].sections == {"Volume": [
        ("VolumeName", f"{SAMPLE_PREFIX}derivatives"), label,
        ("Device", SAMPLE_DERIVATIVES_DIR), ("Type", "none"), ("Options", "bind"),
    ]}
    # Nothing else changes: the containers mount the volume by its unit either way.
    assert {key: unit for key, unit in bound.items() if key != name} == {
        key: unit for key, unit in plain.items() if key != name
    }
    assert SAMPLE_DERIVATIVES_DIR not in "".join(rendered().values())


@pytest.mark.parametrize("change", ["missing", "reordered", "repeated", "apart"])
def test_the_derivatives_bind_lines_must_be_exactly_the_three_together(change: str) -> None:
    installer = load_installer()
    templates = real_templates()
    text = templates["derivatives.volume"]
    bind = "Device=@@DERIVATIVES_DIR@@\nType=none\nOptions=bind\n"
    assert bind in text
    edited = {
        "missing": bind.replace("Type=none\n", ""),
        "reordered": "Type=none\nDevice=@@DERIVATIVES_DIR@@\nOptions=bind\n",
        "repeated": bind + "Options=bind\n",
        "apart": "Device=@@DERIVATIVES_DIR@@\n# between\nType=none\nOptions=bind\n",
    }[change]
    templates["derivatives.volume"] = text.replace(bind, edited)

    for directory in (None, SAMPLE_DERIVATIVES_DIR):
        with pytest.raises(installer.RenderError, match="bind lines"):
            installer.render_units(sample_config(derivatives_dir=directory), templates)


def test_the_derivatives_directory_token_has_no_value_without_a_directory() -> None:
    installer = load_installer()
    templates = real_templates()
    templates["media.container"] += "# @@DERIVATIVES_DIR@@\n"

    with pytest.raises(installer.RenderError, match="unknown token @@DERIVATIVES_DIR@@"):
        installer.render_units(sample_config(), templates)
    assert installer.render_units(sample_config(derivatives_dir=SAMPLE_DERIVATIVES_DIR), templates)


def test_the_renderer_interface_names_the_media_image_and_the_derivatives_directory() -> None:
    installer = load_installer()

    # An installer renders with each release's own copy of this interface (RENDER_API).
    assert installer.RENDER_API == 2 and installer.RENDER_APIS == (1, 2)
    fields = {field.name for field in dataclasses.fields(installer.UnitConfig)}
    assert {"image_media", "derivatives_dir"} <= fields
    assert installer.UnitConfig.__dataclass_fields__["derivatives_dir"].default is None


def test_every_container_shares_the_install_level_and_nothing_else_names_one() -> None:
    rendered_units = units()
    for name in CONTAINERS:
        assert container(name).values("Container", "SecurityLabelLevel") == [
            SAMPLE_LABEL_LEVEL,
        ], name
    for name, unit in rendered_units.items():
        if not name.endswith(".container"):
            assert all("SecurityLabelLevel" not in unit.keys(part) for part in unit.sections)
    other = "s0:c5,c600"
    assert all(
        unit.values("Container", "SecurityLabelLevel") == [other]
        for name, unit in units(label_level=other).items() if name.endswith(".container")
    )
