from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = list(
    dict.fromkeys([*RUNTIME_CONFIG.allowed_hosts, "localhost", "127.0.0.1"])  # noqa: F405
)
