"""The one local Podman command form used by the helper (never a remote connection)."""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

Runner = Callable[..., subprocess.CompletedProcess[str]]


def run(arguments: Sequence[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["podman", "--remote=false", *arguments],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
