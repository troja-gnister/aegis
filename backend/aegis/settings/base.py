import os
from datetime import timedelta
from pathlib import Path

from aegis.config import RuntimeConfig, read_secret

BASE_DIR = Path(__file__).resolve().parents[2]

_runtime_environ = dict(os.environ)
if _runtime_environ.get("AEGIS_ENV", "development").strip().lower() != "production":
    _runtime_environ.setdefault("AEGIS_DJANGO_SECRET_KEY", "development-only-secret-key")
    _runtime_environ.setdefault("AEGIS_DB_PASSWORD", "development-only-database-password")
RUNTIME_CONFIG = RuntimeConfig.from_environ(_runtime_environ)
AEGIS_AUTH_THROTTLE_HMAC_KEY = read_secret(
    _runtime_environ,
    "AEGIS_AUTH_THROTTLE_HMAC_KEY",
    production=RUNTIME_CONFIG.environment == "production",
    required=False,
)

AEGIS_ENVIRONMENT = RUNTIME_CONFIG.environment
SECRET_KEY = RUNTIME_CONFIG.django_secret_key
DEBUG = False
ALLOWED_HOSTS = list(RUNTIME_CONFIG.allowed_hosts)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "aegis_apps.common",
    "aegis_apps.identity",
    "aegis_apps.audit",
]

MIDDLEWARE = [
    "aegis_apps.common.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "aegis_apps.identity.session_policy.SessionPolicyMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "aegis.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

ASGI_APPLICATION = "aegis.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": RUNTIME_CONFIG.db_name,
        "USER": RUNTIME_CONFIG.db_user,
        "PASSWORD": RUNTIME_CONFIG.db_password,
        "HOST": RUNTIME_CONFIG.db_host,
        "PORT": RUNTIME_CONFIG.db_port,
    }
}

AUTH_USER_MODEL = "identity.User"

AEGIS_SESSION_IDLE_AGE = timedelta(minutes=30)
AEGIS_SESSION_ABSOLUTE_AGE = timedelta(hours=12)
AEGIS_SESSION_ACTIVITY_WRITE_INTERVAL = timedelta(minutes=1)

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = RUNTIME_CONFIG.secure_cookies
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = RUNTIME_CONFIG.secure_cookies
CSRF_FAILURE_VIEW = "aegis_apps.identity.api.csrf_failure"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/admin-static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "aegis_json": {"()": "aegis_apps.common.logging.BoundedJSONFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "aegis_json",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.server": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
