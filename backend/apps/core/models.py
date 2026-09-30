"""Core models: AuditLogs, Notifications (shared), real-time broadcast helper (§17, §24, §30)."""
import json

from django.conf import settings
from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """Append-only security/operations trail (§30). Never stores secrets."""

    ACTIONS = [
        ("login", "Login"), ("login_failed", "Failed login"), ("logout", "Logout"),
        ("register", "Registration"), ("password_reset", "Password reset"),
        ("2fa_enabled", "2FA enabled"), ("2fa_disabled", "2FA disabled"),
        ("platform_connected", "Platform connected"), ("platform_disconnected", "Platform disconnected"),
        ("token_refreshed", "OAuth token refreshed"), ("token_expired", "OAuth token expired"),
        ("stream_started", "Stream started"), ("stream_stopped", "Stream stopped"),
        ("moderator_invited", "Moderator invited"), ("moderator_revoked", "Moderator revoked"),
        ("role_changed", "Role changed"),
        ("device_authorized", "Device authorized"), ("device_revoked", "Device revoked"),
        ("automation_stopped", "All automation stopped"),
        ("data_exported", "Data exported"), ("account_deleted", "Account deleted"),
        ("comment_deleted", "Comment deleted (privacy)"),
        ("webhook_rejected", "Webhook rejected (signature)"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=64, choices=ACTIONS, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    request_id = models.CharField(max_length=64, blank=True, db_index=True)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)  # sanitized — no tokens/passwords
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "-created_at"]), models.Index(fields=["action"])]

    def __str__(self):
        return f"{self.action} by {self.user_id} @ {self.created_at:%Y-%m-%d %H:%M}"


class Notification(models.Model):
    """In-app notification record; fan-out to FCM/email handled in apps.notifications (§24)."""

    TYPES = [
        ("moderator_invite", "New moderator invitation"),
        ("stream_started", "Stream started"),
        ("stream_ended", "Stream ended"),
        ("platform_disconnected", "Platform disconnected"),
        ("stream_error", "Stream error"),
        ("ai_moderation_alert", "AI moderation alert"),
        ("important_comment", "Important comment"),
        ("device_authorized", "New device authorized"),
        ("security_event", "Security event"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="notifications")
    type = models.CharField(max_length=32, choices=TYPES)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "read_at"])]


def publish(stream_id: int, event_type: str, data: dict):
    """Broadcast a JSON event to everyone watching this stream's WS room (§18).

    Safe to call from Celery workers and API processes alike — goes through the
    Redis channel layer when configured.
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    if layer is None:  # pragma: no cover
        return
    try:
        async_to_sync(layer.group_send)(
            f"stream.{stream_id}",
            {"type": "relay.event", "event": event_type, "data": json.dumps(data, default=str)},
        )
    except Exception:  # never let realtime delivery break ingestion
        import logging

        logging.getLogger("streamforge.realtime").exception("publish failed for stream %s", stream_id)
