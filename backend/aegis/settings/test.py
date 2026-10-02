import os

from .base import *  # noqa: F403

AEGIS_ENVIRONMENT = "test"
# Ordinary test runs (the verification runner strips inherited AEGIS_* inputs) keep a
# fixed key. A disposable test deployment that provides its own protected key file
# (the e2e and benchmark Compose stacks) signs sessions and cursors with that key.
SECRET_KEY = (
    RUNTIME_CONFIG.django_secret_key  # noqa: F405
    if os.environ.get("AEGIS_DJANGO_SECRET_KEY_FILE")
    else "test-only-secret-key"
)
AEGIS_AUTH_THROTTLE_HMAC_KEY = "test-only-auth-throttle-hmac-key-" + "a" * 32
E2E_ALICE_PASSWORD_FILE = os.environ.get("E2E_ALICE_PASSWORD_FILE", "")
E2E_BOB_PASSWORD_FILE = os.environ.get("E2E_BOB_PASSWORD_FILE", "")
E2E_ADMIN_PASSWORD_FILE = os.environ.get("E2E_ADMIN_PASSWORD_FILE", "")
