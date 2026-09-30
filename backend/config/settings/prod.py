"""Production settings — hardened, env-driven (§30, §34)."""
from .base import *  # noqa: F401,F403

DEBUG = False

# Fail fast if required secrets are missing.
REQUIRED_ENV = ["SECRET_KEY", "TOKEN_ENCRYPTION_KEY", "DATABASE_URL_OR_PARAMS", "REDIS_URL"]
if not SECRET_KEY:
    raise ImproperlyConfigured("SECRET_KEY must be set in the environment")  # noqa: F821
if not TOKEN_ENCRYPTION_KEY:
    raise ImproperlyConfigured(
        "TOKEN_ENCRYPTION_KEY (base64 32-byte AES key) must be set in production"
    )

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
