from urllib.parse import urlparse

from aegis.config import ConfigurationError

from .base import *  # noqa: F403

if RUNTIME_CONFIG.environment != "production":  # noqa: F405
    raise ConfigurationError("Production settings require AEGIS_ENV=production")

ALLOWED_HOSTS = list(RUNTIME_CONFIG.allowed_hosts)  # noqa: F405
_public_url = urlparse(RUNTIME_CONFIG.public_url)  # noqa: F405
CSRF_TRUSTED_ORIGINS = [f"{_public_url.scheme}://{_public_url.netloc}"]

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_SSL_REDIRECT = True

if RUNTIME_CONFIG.trust_proxy_headers:  # noqa: F405
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
