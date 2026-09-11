from .base import *  # noqa: F403

AEGIS_ENVIRONMENT = "test"
SECRET_KEY = "test-only-secret-key"
AEGIS_AUTH_THROTTLE_HMAC_KEY = "test-only-auth-throttle-hmac-key-" + "a" * 32
