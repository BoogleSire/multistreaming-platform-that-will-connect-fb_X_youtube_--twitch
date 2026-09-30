"""Root URL configuration — versioned API (§31) + webhook receivers (§32).

Honesty note (§46): several feature apps from the roadmap (streams views, comments, AI,
moderation, devices, notifications, analytics, core) do not ship their modules yet. Their
routes are therefore mounted conditionally so `manage.py check`/runserver stay green and the
already-implemented surface works; uncomment-free auto-detection keeps this file truthful as
each phase lands its urls module.
"""
import importlib.util

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),  # internal staff admin (§40 seed)
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/platforms/", include("apps.platforms.urls")),
    path("api/v1/streams/", include("apps.clips.urls")),   # Clips Studio (docs/IMPROVEMENT_PROPOSAL.md)
    path("webhooks/youtube/", include("apps.platforms.webhook_urls_youtube")),
    path("webhooks/facebook/", include("apps.platforms.webhook_urls_facebook")),
    path("webhooks/twitch/", include("apps.platforms.webhook_urls_twitch")),
]

# Routes for phases that have not shipped their urlconf yet activate automatically.
_PENDING = {
    "api/v1/comments/": "apps.comments.urls",
    "api/v1/ai/": "apps.ai.urls",
    "api/v1/moderators/": "apps.moderation.urls",
    "api/v1/devices/": "apps.devices.urls",
    "api/v1/notifications/": "apps.notifications.urls",
    "api/v1/analytics/": "apps.analytics.urls",
    "api/v1/core/": "apps.core.urls",
}
for prefix, module in _PENDING.items():
    if importlib.util.find_spec(module) is not None:
        urlpatterns.append(path(prefix, include(module)))
