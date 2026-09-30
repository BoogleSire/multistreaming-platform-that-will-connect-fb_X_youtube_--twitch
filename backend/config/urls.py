"""Root URL configuration — versioned API (§31) + webhook receivers (§32)."""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),  # internal staff admin (§40 seed)
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/platforms/", include("apps.platforms.urls")),
    path("api/v1/streams/", include("apps.streams.urls")),
    path("api/v1/comments/", include("apps.comments.urls")),
    path("api/v1/ai/", include("apps.ai.urls")),
    path("api/v1/moderators/", include("apps.moderation.urls")),
    path("api/v1/devices/", include("apps.devices.urls")),
    path("api/v1/notifications/", include("apps.notifications.urls")),
    path("api/v1/analytics/", include("apps.analytics.urls")),
    path("api/v1/core/", include("apps.core.urls")),
    # Platform webhooks live outside the authed API surface; they verify signatures instead.
    path("webhooks/youtube/", include("apps.platforms.webhook_urls_youtube")),
    path("webhooks/facebook/", include("apps.platforms.webhook_urls_facebook")),
    path("webhooks/twitch/", include("apps.platforms.webhook_urls_twitch")),
]
