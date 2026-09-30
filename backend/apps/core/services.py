"""Helpers for writing audit rows and emitting notifications from anywhere (§30, §24)."""
from django.contrib.auth.signals import (
    user_logged_in, user_logged_out, user_login_failed,
)
from django.dispatch import receiver
from django.utils import timezone

from .middleware import current_request
from .models import AuditLog, Notification, publish


def audit(action, user=None, *, target=None, metadata=None, request=None):
    req = request or current_request.get()
    AuditLog.objects.create(
        user=user if getattr(user, "pk", None) else None,
        action=action,
        ip_address=_client_ip(req) if req else None,
        user_agent=(req.headers.get("User-Agent", "")[:500] if req else ""),
        request_id=getattr(req, "request_id", "") if req else "",
        target_type=type(target).__name__ if target else "",
        target_id=str(getattr(target, "pk", "")) if target else "",
        metadata=_sanitize(metadata or {}),
    )


_SECRET_KEYS = {"password", "token", "access_token", "refresh_token", "secret", "code", "authorization"}


def _sanitize(meta: dict) -> dict:
    """Recursively strip anything that smells like a credential before it hits the log (§30)."""
    clean = {}
    for k, v in meta.items():
        if any(s in k.lower() for s in _SECRET_KEYS):
            continue
        clean[k] = _sanitize(v) if isinstance(v, dict) else v
    return clean


def _client_ip(request):
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")


def notify(user, ntype, title, body="", payload=None, stream_id=None):
    n = Notification.objects.create(
        user=user, type=ntype, title=title, body=body, payload=payload or {}
    )
    # Realtime delivery to the affected user's personal room + optional stream room.
    publish_personal(user.id, "notification.new", {"id": n.id, "type": ntype, "title": title})
    if stream_id:
        publish(stream_id, "notification.new", {"user_id": user.id, "type": ntype, "title": title})
    # Push/email dispatch is a background job so requests stay fast.
    from apps.notifications.tasks import dispatch_notification

    dispatch_notification.delay(n.id)
    return n


def publish_personal(user_id: int, event_type: str, data: dict):
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            f"user.{user_id}",
            {"type": "relay.event", "event": event_type, "data": _json(data)},
        )
    except Exception:  # pragma: no cover
        pass


def _json(data):
    import json

    return json.dumps(data, default=str)


# ------------------------------------------------------------------ auth security events (§30)
@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    audit("login", user=user, request=request)
    notify(user, "security_event", "New sign-in detected",
           f"Your account signed in from {_client_ip(request) or 'an unknown IP'} at "
           f"{timezone.localtime().strftime('%H:%M %Z')}. If this wasn't you, change your password.")


@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    if user:
        audit("logout", user=user, request=request)


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request=None, **kwargs):
    audit("login_failed", metadata={"attempted_username": credentials.get("username", "")},
          request=request)
