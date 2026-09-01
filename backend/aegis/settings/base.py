import os
from pathlib import Path

from aegis.config import RuntimeConfig

BASE_DIR = Path(__file__).resolve().parents[2]

_runtime_environ = dict(os.environ)
if _runtime_environ.get("AEGIS_ENV", "development").strip().lower() != "production":
    _runtime_environ.setdefault("AEGIS_DJANGO_SECRET_KEY", "development-only-secret-key")
    _runtime_environ.setdefault("AEGIS_DB_PASSWORD", "development-only-database-password")
RUNTIME_CONFIG = RuntimeConfig.from_environ(_runtime_environ)

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
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
