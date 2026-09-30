"""
Shared Django settings for StreamForge.

All secrets come from environment variables (.env in development, a secret store in
production). Nothing sensitive is hard-coded — see docs/ENVIRONMENT.md.
"""
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # /workspace/backend
REPO_ROOT = BASE_DIR.parent

load_dotenv(BASE_DIR / ".env")


def env(key: str, default=None):
    val = os.getenv(key)
    return default if val is None or val == "" else val


def env_bool(key: str, default: bool = False) -> bool:
    return str(env(key, str(default))).lower() in ("1", "true", "yes", "on")


def env_list(key: str, default=""):
    raw = env(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# ------------------------------------------------------------------ core
SECRET_KEY = env("SECRET_KEY")  # required — startup check below enforces it
DEBUG = False
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")

INSTALLED_APPS = [
    "daphne",  # ASGI server must precede staticfiles for Channels runserver
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # third-party
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
    "channels",
    "django_filters",
    # project
    "apps.core",
    "apps.accounts",
    "apps.platforms",
    "apps.streams",
    "apps.clips",
    "apps.comments",
    "apps.ai",
    "apps.moderation",
    "apps.devices",
    "apps.analytics",
    "apps.notifications",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.AuditContextMiddleware",
    "apps.core.middleware.RequestIDMiddleware",
]

ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"
WSGI_APPLICATION = "config.wsgi.application"

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

# ------------------------------------------------------------------ database (§17, §33)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "streamforge"),
        "USER": env("DB_USER", "streamforge"),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", "db"),
        "PORT": env("DB_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

# ------------------------------------------------------------------ auth (§16, §30)
AUTH_USER_MODEL = "accounts.User"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Argon2 time/memory cost (defence-in-depth against GPU cracking)
ARGON2_PASS_COST = int(env("ARGON2_TIME_COST", "3"))

# ------------------------------------------------------------------ JWT / sessions
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(env("JWT_ACCESS_MINUTES", "15"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(env("JWT_REFRESH_DAYS", "14"))),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": env("JWT_SECRET", SECRET_KEY),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "TOKEN_TYPE_CLAIM": "token_type",
}

# ------------------------------------------------------------------ REST framework (§31, §30)
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "apps.accounts.authentication.CookieJWTAuthentication",
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.CursorPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("THROTTLE_ANON", "60/min"),
        "user": env("THROTTLE_USER", "600/hour"),
        "auth_burst": env("THROTTLE_AUTH", "10/min"),
        "ai": env("THROTTLE_AI", "30/min"),
    },
    "EXCEPTION_HANDLER": "apps.core.exceptions.streamforge_exception_handler",
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    "NON_FIELD_ERRORS_KEY": "detail",
}

# ------------------------------------------------------------------ channels / redis (§18)
REDIS_URL = env("REDIS_URL", "redis://redis:6379/0")
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL]},
    },
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env("CACHE_REDIS_URL", REDIS_URL),
    }
}

# ------------------------------------------------------------------ celery (§18)
CELERY_BROKER_URL = env("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", REDIS_URL)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_TASK_DEFAULT_QUEUE = "default"
CELERY_BEAT_SCHEDULE = {
    "poll-comments-every-5s": {
        "task": "apps.comments.tasks.poll_all_active_streams",
        "schedule": float(env("COMMENT_POLL_SECONDS", "5")),
    },
    "refresh-expiring-tokens-hourly": {
        "task": "apps.platforms.tasks.refresh_expiring_tokens",
        "schedule": 3600.0,
    },
    "collect-stream-health-every-10s": {
        "task": "apps.streams.tasks.collect_stream_health",
        "schedule": 10.0,
    },
    "sweep-expired-clips-daily": {
        "task": "apps.clips.tasks.sweep_expired_clips",
        "schedule": 86400.0,
    },
}

# ------------------------------------------------------------------ security headers (§30)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # read by SPA to set X-CSRFToken header
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "https://localhost")
SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "https://localhost:3000")
CORS_ALLOW_CREDENTIALS = True

# ------------------------------------------------------------------ token encryption (§17/§30)
# AES-256-GCM key (32-byte base64) used to encrypt OAuth tokens at rest.
TOKEN_ENCRYPTION_KEY = env("TOKEN_ENCRYPTION_KEY", "")

# ------------------------------------------------------------------ AI providers (§35, §36)
AI_PROVIDER = env("AI_PROVIDER", "openai")  # openai | anthropic | google | local | disabled
OPENAI_API_KEY = env("OPENAI_API_KEY", "")
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
GOOGLE_API_KEY = env("GOOGLE_API_KEY", "")
GOOGLE_MODEL = env("GOOGLE_MODEL", "gemini-1.5-flash")
LOCAL_AI_BASE_URL = env("LOCAL_AI_BASE_URL", "")  # e.g. http://ollama:11434
AI_DEFAULT_MAX_RESPONSES_PER_MINUTE = int(env("AI_MAX_PER_MINUTE", "12"))
AI_DEFAULT_MIN_CONFIDENCE = float(env("AI_MIN_CONFIDENCE", "0.6"))
AI_CACHE_TTL_SECONDS = int(env("AI_CACHE_TTL", "3600"))

# ------------------------------------------------------------------ streaming (§3, §22)
INGEST_RTMP_URL = env("INGEST_RTMP_URL", "rtmp://ingest.local/live")
INGEST_RTMPSS_URL = env("INGEST_RTMPS_URL", "rtmps://ingest.local:443/live")
HLS_BASE_URL = env("HLS_BASE_URL", "https://media.local/live")
MEDIAMTX_API_URL = env("MEDIAMTX_API_URL", "http://ingest:9997")
FFMPEG_BIN = env("FFMPEG_BIN", "/usr/bin/ffmpeg")
STREAM_KEY_ROTATION_DAYS = int(env("STREAM_KEY_ROTATION_DAYS", "30"))

# ------------------------------------------------------------------ Clips Studio (docs/IMPROVEMENT_PROPOSAL.md)
# Rolling DVR recording written by the ingest recorder: {CLIP_DVR_ROOT}/{stream_id}/segment-<epoch>.ts
CLIP_DVR_ROOT = env("CLIP_DVR_ROOT", str(BASE_DIR / "dvr"))
CLIP_WORK_ROOT = env("CLIP_WORK_ROOT", str(BASE_DIR / "tmp" / "clips"))
CLIP_SEGMENT_SECONDS = int(env("CLIP_SEGMENT_SECONDS", "10"))     # recorder segment length
CLIP_RENDER_TIMEOUT_SECONDS = int(env("CLIP_RENDER_TIMEOUT_SECONDS", "180"))
CLIP_RETENTION_DAYS = int(env("CLIP_RETENTION_DAYS", "30"))       # §38 data retention
FFPROBE_BIN = env("FFPROBE_BIN", "/usr/bin/ffprobe")
# Hype auto-clips (§7 of proposal): opt-in per stream, guarded like AI automation (§12).
HYPE_CLIP_MIN_COMMENTS_PER_15S = int(env("HYPE_CLIP_MIN_COMMENTS", "20"))
HYPE_CLIP_BASELINE_MULTIPLIER = float(env("HYPE_CLIP_BASELINE_MULT", "3.0"))
HYPE_CLIP_COOLDOWN_SECONDS = int(env("HYPE_CLIP_COOLDOWN", "180"))
HYPE_CLIP_LOOKBACK_SECONDS = int(env("HYPE_CLIP_LOOKBACK", "90"))

# ------------------------------------------------------------------ platform OAuth (§2, §34)
YOUTUBE_CLIENT_ID = env("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = env("YOUTUBE_CLIENT_SECRET", "")
FACEBOOK_CLIENT_ID = env("FACEBOOK_CLIENT_ID", "")
FACEBOOK_CLIENT_SECRET = env("FACEBOOK_CLIENT_SECRET", "")
TWITCH_CLIENT_ID = env("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = env("TWITCH_CLIENT_SECRET", "")
X_CLIENT_ID = env("X_CLIENT_ID", "")
X_CLIENT_SECRET = env("X_CLIENT_SECRET", "")
TIKTOK_CLIENT_ID = env("TIKTOK_CLIENT_ID", "")
TIKTOK_CLIENT_SECRET = env("TIKTOK_CLIENT_SECRET", "")
INSTAGRAM_CLIENT_ID = env("INSTAGRAM_CLIENT_ID", "")
INSTAGRAM_CLIENT_SECRET = env("INSTAGRAM_CLIENT_SECRET", "")
OAUTH_REDIRECT_BASE = env("OAUTH_REDIRECT_BASE", "https://api.local")
WEBHOOK_SECRET = env("WEBHOOK_SECRET", "")  # HMAC key we share with YouTube push etc.

# Development-only simulated integrations (§46). Default OFF everywhere.
MOCK_PLATFORM_ENABLED = env_bool("MOCK_PLATFORM_ENABLED", False)

# ------------------------------------------------------------------ notifications (§24)
FCM_SERVER_KEY = env("FCM_SERVER_KEY", "")
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "no-reply@streamforge.app")
FRONTEND_BASE_URL = env("FRONTEND_BASE_URL", "https://app.local")

# ------------------------------------------------------------------ misc
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "apps.core.logging.JsonFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "streamforge": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO"), "propagate": False},
    },
}

# Subscription architecture placeholder (§41) — billing intentionally NOT enabled.
SUBSCRIPTION_PLANS = {
    "free": {"max_platforms": 1, "ai_responses_per_day": 50, "moderators": 0},
    "creator": {"max_platforms": 4, "ai_responses_per_day": 2000, "moderators": 5},
    "pro": {"max_platforms": 10, "ai_responses_per_day": 20000, "moderators": 25},
}
