import os

from .base import *  # noqa: F403

AEGIS_ENVIRONMENT = "test"
SECRET_KEY = "test-only-secret-key"
AEGIS_AUTH_THROTTLE_HMAC_KEY = "test-only-auth-throttle-hmac-key-" + "a" * 32
E2E_ALICE_PASSWORD_FILE = os.environ.get("E2E_ALICE_PASSWORD_FILE", "")
E2E_BOB_PASSWORD_FILE = os.environ.get("E2E_BOB_PASSWORD_FILE", "")
E2E_ADMIN_PASSWORD_FILE = os.environ.get("E2E_ADMIN_PASSWORD_FILE", "")
