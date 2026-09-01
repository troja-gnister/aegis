import os
from pathlib import Path

import pytest
from aegis_apps.identity.validators import read_private_secret


def test_private_secret_must_not_be_group_or_world_readable(tmp_path: Path) -> None:
    path = tmp_path / "admin-password"
    path.write_text("correct horse battery staple\n")
    os.chmod(path, 0o640)

    with pytest.raises(ValueError, match="mode 0600"):
        read_private_secret(path)


def test_private_secret_strips_one_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / "admin-password"
    path.write_text("correct horse battery staple\n")
    os.chmod(path, 0o600)

    assert read_private_secret(path) == "correct horse battery staple"
