"""Compose's own rendering of the deployment files, through the checked launcher."""
from __future__ import annotations

import json
import os
from typing import Any, cast

from aegisctl.container_engine import compose_environment as validated_compose_environment
from aegisctl.container_engine import container_command

from tests.support.container_runtime import run_deployment_process as run_container


def rendered_compose(
    *profiles: str,
    environment: dict[str, str] | None = None,
    files: tuple[str, ...] = ("compose.yaml",),
) -> dict[str, Any]:
    profile_arguments = [argument for profile in profiles for argument in ("--profile", profile)]
    file_arguments = [argument for name in files for argument in ("-f", name)]
    environment_for_compose = (
        os.environ
        | {"AEGIS_RELEASE_ID": "test-release-identity"}
        | (environment or {})
    )
    result = run_container(
        [
            *container_command(),
            "compose",
            *profile_arguments,
            *file_arguments,
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=validated_compose_environment(environment_for_compose),
    )
    return cast(dict[str, Any], json.loads(result.stdout))
