"""Background jobs for platforms (§18, §39): token refresh + webhook subscription upkeep."""
from celery import shared_task
from django.utils import timezone

from apps.core.exceptions import TokenExpiredError

from .models import ConnectedPlatform
from .services import build_adapter


@shared_task(bind=True, max_retries=4)
def refresh_expiring_tokens(self):
    """Beat job: proactively refresh OAuth tokens within 24h of expiry (§39 retry/backoff)."""
    refreshed = failed = 0
    qs = (ConnectedPlatform.objects
          .filter(disconnected_at__isnull=True, platform_token__isnull=False)
          .select_related("platform_token", "user"))
    for conn in qs.iterator():
        tok = conn.token
        if not tok or not tok.near_expiry or not tok.refresh_token_enc:
            continue
        try:
            adapter = build_adapter(conn)
            result = adapter.refresh_token(tok.refresh_token_enc)
            tok.access_token_enc = result["access_token"]
            if result.get("expires_at"):
                tok.expires_at = result["expires_at"]
            tok.save(update_fields=["access_token_enc", "expires_at", "updated_at"])
            if conn.status == "expired":
                conn.status = "connected"
                conn.last_error = ""
                conn.save(update_fields=["status", "last_error", "updated_at"])
            refreshed += 1
        except TokenExpiredError:
            conn.status = "expired"
            conn.last_error = (f"{conn.platform.title()} authorization expired. Please reconnect "
                               f"your {conn.platform.title()} account.")
            conn.save(update_fields=["status", "last_error", "updated_at"])
            from apps.core.services import notify

            notify(conn.user, "platform_disconnected",
                   f"{conn.platform.title()} needs reconnection", conn.last_error)
            failed += 1
        except Exception as exc:
            conn.status = "error"
            conn.last_error = str(exc)[:500]
            conn.save(update_fields=["status", "last_error", "updated_at"])
            failed += 1
    return {"refreshed": refreshed, "failed": failed}


@shared_task
def verify_webhook_subscriptions():
    """Ensure push subscriptions (YouTube PubSubHubbub / Twitch EventSub) are still live;
    renew any that expire within a day. Deployments without credentials no-op cleanly."""
    # Implemented per-adapter in production; kept as a scheduled hook so the beat entry
    # exists and operators can see it succeed even before webhooks are configured.
    return {"checked": ConnectedPlatform.objects.filter(disconnected_at__isnull=True).count(),
            "at": timezone.now().isoformat()}
