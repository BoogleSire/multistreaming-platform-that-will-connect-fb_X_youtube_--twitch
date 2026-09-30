"""Webhook receivers (§32) — signature-verified, never trusted blindly.

Every handler:
1. Verifies the platform signature (adapter.validate_webhook) BEFORE touching data.
2. Enqueues processing to Celery (fast 200 response; retries live in the queue).
3. Rejects unauthenticated deliveries with 403 + audit row.
"""
import json

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.core.services import audit
from apps.core.integrations.base import load_all_adapters, get_integration_class


@require_GET
def youtube_push_verify(request):
    """PubSubHubbub subscription handshake: echo hub.challenge when topics match."""
    mode = request.GET.get("hub.mode")
    topic = request.GET.get("hub.topic", "")
    challenge = request.GET.get("hub.challenge", "")
    verify_token = request.GET.get("hub.verify_token", "")
    from django.conf import settings

    if mode in ("subscribe", "unsubscribe") and verify_token == settings.WEBHOOK_SECRET:
        return HttpResponse(challenge, content_type="text/plain")
    audit("webhook_rejected", metadata={"platform": "youtube", "stage": "verify"})
    return HttpResponseForbidden_view()


def HttpResponseForbidden_view():
    return JsonResponse({"detail": "Webhook verification failed."}, status=403)


@csrf_exempt
@require_POST
def youtube_push_callback(request):
    load_all_adapters()
    adapter_cls = get_integration_class("youtube")
    ok = adapter_cls({}).validate_webhook(dict(request.headers), request.body)
    if not ok:
        audit("webhook_rejected", metadata={"platform": "youtube"})
        return JsonResponse({"detail": "Invalid webhook signature."}, status=403)
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"detail": "Malformed payload."}, status=400)
    from apps.comments.tasks import process_platform_notification

    for entry in payload.get("entries", []):
        process_platform_notification.delay("youtube", entry)
    return JsonResponse({"status": "queued"})


@csrf_exempt
def facebook_webhook(request):
    """Meta page subscriptions: GET handshake (hub.verify_token == app secret) + POST events."""
    if request.method == "GET":
        from django.conf import settings

        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge", "")
        if mode == "subscribe" and token == settings.FACEBOOK_CLIENT_SECRET:
            return HttpResponse(challenge, content_type="text/plain")
        audit("webhook_rejected", metadata={"platform": "facebook", "stage": "verify"})
        return JsonResponse({"detail": "Verification failed."}, status=403)

    load_all_adapters()
    adapter_cls = get_integration_class("facebook")
    if not adapter_cls({}).validate_webhook(dict(request.headers), request.body):
        audit("webhook_rejected", metadata={"platform": "facebook"})
        return JsonResponse({"detail": "Invalid webhook signature."}, status=403)
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"detail": "Malformed payload."}, status=400)
    from apps.comments.tasks import process_platform_notification

    process_platform_notification.delay("facebook", payload)
    return JsonResponse({"status": "queued"})


@csrf_exempt
@require_POST
def twitch_eventsub(request):
    load_all_adapters()
    adapter_cls = get_integration_class("twitch")
    message_type = request.headers.get("Twitch-Eventsub-Message-Type", "")
    if not adapter_cls({}).validate_webhook(dict(request.headers), request.body):
        audit("webhook_rejected", metadata={"platform": "twitch", "type": message_type})
        return JsonResponse({"detail": "Invalid EventSub signature."}, status=403)
    payload = json.loads(request.body)
    if message_type == "session_challenge":  # subscription confirmation
        return JsonResponse({"challenge": payload.get("challenge")})
    from apps.comments.tasks import process_platform_notification

    process_platform_notification.delay("twitch", payload)
    return JsonResponse({"status": "queued"}, status=200)


def _forbid():
    return JsonResponse({"detail": "Webhook authentication failed."}, status=403)
