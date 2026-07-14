"""
Django settings for the Artikate Studio backend assessment.

Three concerns are wired up here, one per assessment section:
  * django-silk (Section 1) — SQL profiling / query-count evidence.
  * Celery + Redis  (Section 2) — async job queue + rate limiting.
  * Tenant middleware (Section 3) — automatic per-request tenant scoping.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Secret key is read from the environment so nothing sensitive is committed.
# The insecure fallback keeps `runserver`/tests working out of the box for review.
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-assessment-only-do-not-use-in-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = ["*"]  # dev/assessment only; subdomain routing needs this (Section 3)


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "silk",
    # Local apps (one per assessment section)
    "orders",     # Section 1 — N+1 diagnosis
    "emailq",     # Section 2 — rate-limited async job queue
    "tenancy",    # Section 3 — multi-tenant data isolation
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Section 3: binds the tenant for the whole request lifecycle and clears it
    # afterwards. Placed last so request.user (from AuthenticationMiddleware) is
    # already available if we ever want to derive the tenant from the user.
    "tenancy.middleware.TenantMiddleware",
    # Section 1: silk wraps the SQL cursor to record queries. Kept last so it
    # measures everything the other middleware also triggers.
    "silk.middleware.SilkyMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

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
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Redis (shared by Celery broker/result backend and the rate limiter)
# ---------------------------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")


# ---------------------------------------------------------------------------
# Section 2 — Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# --- Crash-safety: the core of the SIGKILL answer ---
# acks_late=True: a task is acknowledged to the broker only AFTER it finishes,
# not when the worker picks it up. If the worker is SIGKILL'd mid-run, Redis
# never receives the ack and re-delivers the task to another worker.
CELERY_TASK_ACKS_LATE = True
# If the worker process dies (not a normal exception), re-queue the task.
CELERY_TASK_REJECT_ON_WORKER_LOST = True
# Fetch one task at a time so a killed worker loses at most one in-flight job
# instead of a whole prefetched batch.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"
# Surface broker connection errors at startup instead of retrying silently.
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# Rate-limiter configuration (Section 2). 200 emails / 60s window.
EMAIL_RATE_LIMIT = int(os.environ.get("EMAIL_RATE_LIMIT", "200"))
EMAIL_RATE_WINDOW_SECONDS = int(os.environ.get("EMAIL_RATE_WINDOW_SECONDS", "60"))


# ---------------------------------------------------------------------------
# Section 3 — tenant resolution
# ---------------------------------------------------------------------------
# HS256 secret used to sign/verify the demo JWT that carries the tenant claim.
TENANT_JWT_SECRET = os.environ.get("TENANT_JWT_SECRET", "assessment-tenant-jwt-secret")
TENANT_JWT_ALGORITHM = "HS256"
# Base domain used to derive tenant from subdomain, e.g. acme.artikate.test -> "acme".
TENANT_BASE_DOMAIN = os.environ.get("TENANT_BASE_DOMAIN", "artikate.test")


REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

# Silk: record every request while DEBUG so before/after query counts are easy
# to capture. In production you would sample instead of recording everything.
SILKY_PYTHON_PROFILER = False
SILKY_META = True
