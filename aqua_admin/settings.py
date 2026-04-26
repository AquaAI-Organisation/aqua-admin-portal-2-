"""Django settings for the Aqua AI admin control plane."""
import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.getenv("DEBUG", "False").lower() == "true"

# ALLOWED_HOSTS: accept anything if '*' or empty, otherwise split on comma
_hosts = os.getenv("ALLOWED_HOSTS", "*")
if _hosts.strip() == "*" or not _hosts.strip():
    ALLOWED_HOSTS = ["*"]
else:
    ALLOWED_HOSTS = [h.strip() for h in _hosts.split(",") if h.strip()]

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

# Database — use DATABASE_URL from environment (Heroku sets this automatically
# for Heroku Postgres; we set it manually for Supabase).
_db_url = os.getenv("DATABASE_URL", "sqlite:///" + str(BASE_DIR / "db.sqlite3"))
DATABASES = {
    "default": dj_database_url.parse(
        _db_url,
        conn_max_age=60,
    )
}
# Force SSL for Postgres connections (Supabase requires it)
if _db_url.startswith("postgres"):
    DATABASES["default"]["OPTIONS"] = {"sslmode": "require"}

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

# Static files — WhiteNoise serves them on Heroku
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "admin_portal" / "static"]

# Use WhiteNoise for static file serving (works on Heroku out of the box)
WHITENOISE_USE_FINDERS = True
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/admin-portal/login/"
LOGIN_REDIRECT_URL = "/admin-portal/"
LOGOUT_REDIRECT_URL = "/admin-portal/login/"

SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_TRUSTED_ORIGINS = [
    f"https://{h}" for h in ALLOWED_HOSTS if h != "*"
]

# --- Control-plane specific -------------------------------------------------

SUPERADMIN_EMAILS = [
    e.strip().lower()
    for e in os.getenv("SUPERADMIN_EMAILS", "steven@humara.io,ben@humara.io").split(",")
    if e.strip()
]
LEGACY_ADMIN_REDIRECT_URL = os.getenv("LEGACY_ADMIN_REDIRECT_URL", "https://admin-control.aquaai.uk")
LEGACY_ADMIN_INTERNAL_PATH = os.getenv("LEGACY_ADMIN_INTERNAL_PATH", "/django-internal-admin-8x7k/")

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
