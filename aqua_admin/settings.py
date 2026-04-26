"""Django settings for the Aqua AI admin control plane."""
import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env locally; on Heroku the env vars are set as Config Vars and the
# .env file does not exist, which is fine.
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.getenv("DEBUG", "False").lower() == "true"


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------
_hosts_raw = os.getenv("ALLOWED_HOSTS", "*").strip()
if _hosts_raw == "*" or not _hosts_raw:
    ALLOWED_HOSTS = ["*"]
else:
    ALLOWED_HOSTS = _split_csv(_hosts_raw)
    # Always tolerate Heroku-style hostnames so a fresh deploy just works.
    if not any(h == ".herokuapp.com" or h.endswith(".herokuapp.com") for h in ALLOWED_HOSTS):
        ALLOWED_HOSTS.append(".herokuapp.com")


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "admin_portal",
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

ROOT_URLCONF = "aqua_admin.urls"

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
                "admin_portal.context_processors.branding",
            ],
        },
    },
]

WSGI_APPLICATION = "aqua_admin.wsgi.application"
ASGI_APPLICATION = "aqua_admin.asgi.application"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# DATABASE_URL is set automatically by Heroku for Heroku Postgres; we set it
# manually for Supabase. Always require SSL when speaking to a remote DB.
_db_url = os.getenv("DATABASE_URL", "sqlite:///" + str(BASE_DIR / "db.sqlite3"))
DATABASES = {
    "default": dj_database_url.parse(
        _db_url,
        conn_max_age=60,
        conn_health_checks=True,
        ssl_require=_db_url.startswith("postgres"),
    )
}

AUTH_USER_MODEL = "admin_portal.AdminUser"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
# WhiteNoise serves static files in production. We rely on Django's
# AppDirectoriesFinder to pick up admin_portal/static/* — adding it to
# STATICFILES_DIRS would create duplicates and break `collectstatic`.
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# CompressedStaticFilesStorage (not the *Manifest* variant) is more forgiving
# on Heroku: it won't crash a release if a referenced asset is missing.
WHITENOISE_USE_FINDERS = True
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Auth / sessions
# ---------------------------------------------------------------------------
LOGIN_URL = "/admin-portal/login/"
LOGIN_REDIRECT_URL = "/admin-portal/"
LOGOUT_REDIRECT_URL = "/admin-portal/login/"

SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# ---------------------------------------------------------------------------
# HTTPS behind Heroku's router
# ---------------------------------------------------------------------------
# Heroku terminates TLS at the router and forwards plain HTTP. Tell Django to
# trust the X-Forwarded-Proto header so request.is_secure() returns True.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# ---------------------------------------------------------------------------
# CSRF trusted origins
# ---------------------------------------------------------------------------
# Build https://<host> for every concrete ALLOWED_HOST, plus *.herokuapp.com,
# plus anything the operator wants to add via the CSRF_TRUSTED_ORIGINS env.
def _build_csrf_origins() -> list[str]:
    origins: set[str] = set()
    for host in ALLOWED_HOSTS:
        if not host or host == "*":
            continue
        if host.startswith("."):
            origins.add(f"https://*{host}")
        else:
            origins.add(f"https://{host}")
    origins.add("https://*.herokuapp.com")
    for extra in _split_csv(os.getenv("CSRF_TRUSTED_ORIGINS", "")):
        origins.add(extra if "://" in extra else f"https://{extra}")
    return sorted(origins)


CSRF_TRUSTED_ORIGINS = _build_csrf_origins()

# ---------------------------------------------------------------------------
# Control-plane specific
# ---------------------------------------------------------------------------
SUPERADMIN_EMAILS = [
    e.strip().lower()
    for e in os.getenv("SUPERADMIN_EMAILS", "steven@humara.io,ben@humara.io").split(",")
    if e.strip()
]

# Paste your real OpenAI key into the OPENAI_API_KEY env var. Until you do,
# the dashboard shows a "GPT-4 key missing" indicator and the AI engine
# returns a clear error rather than silently failing.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-REPLACE-WITH-YOUR-GPT-4-KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
AI_APPROVE_THRESHOLD = float(os.getenv("AI_APPROVE_THRESHOLD", "0.80"))
AI_REJECT_THRESHOLD = float(os.getenv("AI_REJECT_THRESHOLD", "0.30"))

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#aqua-admin-alerts")

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() == "true"
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "Aqua Admin <admin@humara.io>")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"std": {"format": "[%(asctime)s] %(levelname)s %(name)s :: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "std"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}
