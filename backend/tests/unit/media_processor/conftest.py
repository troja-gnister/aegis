"""``media_tools`` tests need ffmpeg and ffprobe, which only the aegis-media image has.

They are deselected (and counted as deselected in the summary) unless the ``-m`` expression
names ``media_tools``. Selected without the tools, they fail; they never skip. The end-of-phase
run selects them inside the image: tests/deployment/test_media_image.py.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    expression = config.getoption("markexpr") or ""
    if "media_tools" in expression:
        return
    kept: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        (deselected if item.get_closest_marker("media_tools") else kept).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept
