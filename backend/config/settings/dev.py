"""Development settings — local, honest defaults.

Runs against SQLite if Postgres isn't reachable so `manage.py test` works out of the box,
and uses in-memory channel layer + eager Celery. MOCK_PLATFORM_ENABLED stays OFF unless a
developer explicitly flips it (see docs/ENVIRONMENT.md; requirement §46).
"""
from .base import *  # noqa: F401,F403

DEBUG = env_bool("DEV_DEBUG", True)
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
ALLOWED_HOSTS = ["*"]
CORS_ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]
CSRF_TRUSTED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]

# Fall back to SQLite when Postgres is not configured, for zero-friction local dev/tests.
if not env("DB_HOST") or env("DB_ENGINE_FALLBACK", "sqlite") == "sqlite":
    try:
        import psycopg2  # noqa: F401
        _have_pg = bool(env("DB_PASSWORD"))
    except Exception:
        _have_pg = False
    if not _have_pg:
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": BASE_DIR / "db.sqlite3",
            }
        }

# In-memory channel layer for local dev without Redis.
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

CELERY_TASK_ALWAYS_EAGER = env_bool("DEV_CELERY_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = True

TOKEN_ENCRYPTION_KEY = TOKEN_ENCRYPTION_KEY or "DEVONLY-MDEyMzAxMTIyMzM0NDU1NjY3Nzg4OTkwMDEyMzQ1Ng=="
