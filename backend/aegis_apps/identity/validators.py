from pathlib import Path

from aegis.config import ConfigurationError, read_secret


def read_private_secret(path: Path) -> str:
    try:
        return read_secret(
            {"BOOTSTRAP_PASSWORD_FILE": str(path)},
            "BOOTSTRAP_PASSWORD",
            production=True,
        )
    except ConfigurationError as error:
        raise ValueError(str(error)) from None
