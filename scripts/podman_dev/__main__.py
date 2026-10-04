"""Command line for the Podman development helper: python -m scripts.podman_dev."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from scripts.podman_dev import lifecycle, tools
from scripts.podman_dev.records import SessionError

LOAD_HINT = 'eval "$(uv run --locked python -m scripts.podman_dev env)"'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.podman_dev",
        description="Private rootless Podman development session for Aegis.",
    )
    parser.add_argument("command", choices=("tools", "start", "env", "status", "stop"))
    command = parser.parse_args(argv).command
    home = Path.home()
    try:
        if command == "tools":
            print("\n".join(tools.ensure_tools(home)))
        elif command == "start":
            lifecycle.start(os.environ, home=home)
            print(f"Session started. Load it with:\n  {LOAD_HINT}")
        elif command == "env":
            sys.stdout.write(lifecycle.environment(os.environ, home=home))
        elif command == "status":
            lines, healthy = lifecycle.status(os.environ)
            print("\n".join(lines))
            return 0 if healthy else 1
        else:
            report, unchanged = lifecycle.stop(os.environ)
            print("\n".join(report))
            return 0 if unchanged else 3
    except (SessionError, tools.ToolError) as error:
        print(f"podman_dev: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
