"""Command line for the Podman development helper: python -m scripts.podman_dev."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.podman_dev import tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.podman_dev")
    parser.add_argument("command", choices=("tools",))
    parser.parse_args(argv)
    try:
        print("\n".join(tools.ensure_tools(Path.home())))
    except tools.ToolError as error:
        print(f"podman_dev: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
