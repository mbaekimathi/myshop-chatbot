"""
Django settings for MY-CHATBOT — shop-to-shop chat + data assistant.

Shares the `myshop` MySQL database with the MY-SHOP project. It reads the
existing shop/stock/receipt tables through unmanaged models and owns only
its `chat_*` tables, so both projects can run side by side.
"""

import os
import sys
import warnings
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


APP_ENV = (os.getenv("APP_ENV") or "").strip().lower()
IS_HOSTED = APP_ENV == "production" or (BASE_DIR / ".production").exists()

DEBUG = env_bool("DJANGO_DEBUG", default=not IS_HOSTED)


def _resolve_secret_key() -> str:
    key = (os.getenv("DJANGO_SECRET_KEY") or os.getenv("SECRET_KEY") or "").strip()
    if key:
        return key
    key_file = BASE_DIR / ".secret_key"
    if key_file.exists():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    from django.core.management.utils import get_random_secret_key

    generated = get_random_secret_key()
    try:
        key_file.write_text(generated, encoding="utf-8")
    except OSError:
        pass
    return generated


SECRET_KEY = _resolve_secret_key()

_allowed = (os.getenv("DJANGO_ALLOWED_HOSTS") or "").strip()
ALLOWED_HOSTS = (
    [h.strip() for h in _allowed.split(",") if h.strip()]
    if _allowed
    else (["localhost", "127.0.0.1", "[::1]"] if DEBUG else [])
)

USE_X_FORWARDED_HOST = env_bool("DJANGO_USE_X_FORWARDED_HOST", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

_csrf_origins = (os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS") or "").strip()
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_origins.split(",") if o.strip()]
CSRF_COOKIE_AGE = int(os.getenv("CSRF_COOKIE_AGE", str(60 * 60 * 24 * 365)))

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "chat",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "mychatbot.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "mychatbot.wsgi.application"

# ---------------------------------------------------------------------------
# Database — shared `myshop` schema (owned by MY-SHOP)
# ---------------------------------------------------------------------------

MYSQL_ENABLED = env_bool("MYSQL_ENABLED", default=True)

if MYSQL_ENABLED:
    _mysql_options: dict = {
        "charset": "utf8mb4",
        "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        "connect_timeout": int(os.getenv("MYSQL_CONNECT_TIMEOUT", "5")),
        "read_timeout": int(os.getenv("MYSQL_READ_TIMEOUT", "30")),
        "write_timeout": int(os.getenv("MYSQL_WRITE_TIMEOUT", "30")),
    }
    _mysql_unix_socket = (os.getenv("MYSQL_UNIX_SOCKET") or "").strip()
    if _mysql_unix_socket:
        _mysql_options["unix_socket"] = _mysql_unix_socket

    DATABASES = {
        "default": {
            "ENGINE": "mychatbot.db_backends.mysql",
            "NAME": os.getenv("MYSQL_DATABASE", "myshop"),
            "USER": os.getenv("MYSQL_USER", "root"),
            "PASSWORD": os.getenv("MYSQL_PASSWORD", ""),
            "HOST": os.getenv("MYSQL_HOST", "127.0.0.1"),
            "PORT": os.getenv("MYSQL_PORT", "3306"),
            "OPTIONS": _mysql_options,
            "CONN_MAX_AGE": int(os.getenv("MYSQL_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
    if not DEBUG and "test" not in sys.argv:
        warnings.warn(
            "SQLite is not suitable for production multi-user workloads. "
            "Set MYSQL_ENABLED=True in your environment.",
            stacklevel=1,
        )

# ---------------------------------------------------------------------------
# Cache & sessions
# ---------------------------------------------------------------------------

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "mychatbot-cache",
        "TIMEOUT": int(os.getenv("CACHE_TIMEOUT", "300")),
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_CACHE_ALIAS = "default"
SESSION_COOKIE_AGE = int(os.getenv("SESSION_COOKIE_AGE", str(60 * 60 * 24 * 30)))
SESSION_SAVE_EVERY_REQUEST = False
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LOGIN_URL = "chat:login"
LOGIN_REDIRECT_URL = "chat:app"
LOGOUT_REDIRECT_URL = "chat:login"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

CHAT_ATTACHMENT_MAX_BYTES = int(
    os.getenv("CHAT_ATTACHMENT_MAX_BYTES", str(4 * 1024 * 1024))
)
CHAT_MESSAGE_MAX_LENGTH = int(os.getenv("CHAT_MESSAGE_MAX_LENGTH", "4000"))
CHAT_POLL_ONLINE_SECONDS = int(os.getenv("CHAT_POLL_ONLINE_SECONDS", "25"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOG_LEVEL = os.getenv("DJANGO_LOG_LEVEL", "INFO" if not DEBUG else "DEBUG")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": "[{levelname}] {asctime} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

# ---------------------------------------------------------------------------
# Production hardening (auto when DEBUG=False)
# ---------------------------------------------------------------------------

if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    SECURE_REFERRER_POLICY = "same-origin"
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", default=True)
    SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", default=True)
    CSRF_COOKIE_SECURE = env_bool("CSRF_COOKIE_SECURE", default=True)
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
    if SECURE_HSTS_SECONDS:
        SECURE_HSTS_INCLUDE_SUBDOMAINS = True
        SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
