"""Platforms API (§31): list/connect/callback/disconnect + honest capability matrix."""
from django.urls import path
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.core.integrations.base import available_platforms, load_all_adapters
from apps.core.services import audit

from .models import ConnectedPlatform
from .services import begin_oauth, build_adapter, complete_oauth, disconnect


def _serialize(conn: ConnectedPlatform) -> dict:
    """Public-safe view of a connection. Tokens are NEVER included (§30)."""
    return {
        "id": str(conn.id),
        "platform": conn.platform,
        "account_name": conn.account_name,
        "account_avatar_url": conn.account_avatar_url,
        "status": conn.status,
        "last_error": conn.last_error or None,
        "scopes": conn.scopes,
        "connected_at": conn.connected_at,
        "token_expiring_soon": bool(conn.token and conn.token.near_expiry),
        "manual_stream_key_set": bool((conn.extra or {}).get("manual_stream_key")),
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def platform_catalog(request):
    """All supported integrations + whether this deployment configured them (§2)."""
    load_all_adapters()
    mine = {c.platform: _serialize(c) for c in
            ConnectedPlatform.objects.filter(user=request.user, disconnected_at__isnull=True)}
    catalog = []
    for p in available_platforms():
        row = dict(p)
        row["connection"] = mine.get(p["key"])
        catalog.append(row)
    return Response({"platforms": catalog})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def connect_begin(request, platform):
    url, state = begin_oauth(request.user, platform)
    return Response({"authorization_url": url, "state": state}, status=status.HTTP_200_OK)


@api_view(["GET", "POST"])
@permission_classes([AllowAny])   # authenticated via the signed `state` payload itself (§30)
def callback(request, platform):
    code = request.query_params.get("code") or request.data.get("code")
    state = request.query_params.get("state") or request.data.get("state")
    error = request.query_params.get("error")
    if error:
        return Response({"detail": f"Authorization was cancelled or rejected by the provider ({error}). "
                                   "You can try again from the dashboard."},
                        status=status.HTTP_400_BAD_REQUEST)
    if not (code and state):
        return Response({"detail": "Missing authorization parameters — please restart the connect flow."},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        uid = state.split(":")[0]
    except IndexError:
        return Response({"detail": "Invalid state."}, status=status.HTTP_400_BAD_REQUEST)
    conn = complete_oauth(uid, platform, state, code)
    audit("platform_connected", user=conn.user, target=conn,
          metadata={"platform": platform, "account": conn.account_name})
    # Browser flow: redirect back to the SPA; API client: JSON.
    if "text/html" in request.headers.get("Accept", ""):
        from django.conf import settings
        from django.http import HttpResponseRedirect

        return HttpResponseRedirect(f"{settings.FRONTEND_BASE_URL}/settings/platforms?connected={platform}")
    return Response(_serialize(conn))


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def disconnect_platform(request, pk):
    conn = ConnectedPlatform.objects.filter(pk=pk, user=request.user).first()
    if not conn:
        return Response({"detail": "That connected account wasn't found."},
                        status=status.HTTP_404_NOT_FOUND)
    disconnect(conn)
    audit("platform_disconnected", user=request.user, target=conn,
          metadata={"platform": conn.platform})
    return Response({"detail": f"{conn.platform.title()} disconnected. Stored tokens were deleted."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def set_manual_stream_key(request, pk):
    """Twitch et al.: one-time paste of the user's own stream key (§F). Stored encrypted-ish
    inside ConnectedPlatform.extra (JSON) — visible only to the owner as masked value."""
    conn = ConnectedPlatform.objects.filter(pk=pk, user=request.user).first()
    if not conn:
        return Response({"detail": "Connection not found."}, status=status.HTTP_404_NOT_FOUND)
    key = request.data.get("stream_key", "").strip()
    if not key:
        return Response({"detail": "Provide your stream key."}, status=status.HTTP_400_BAD_REQUEST)
    conn.extra = {**(conn.extra or {}), "manual_stream_key": key}
    conn.save(update_fields=["extra", "updated_at"])
    audit("platform_connected", user=request.user, target=conn,
          metadata={"platform": conn.platform, "stage": "manual_stream_key_set"})
    return Response({"detail": "Stream key saved. It is stored on the server only and never "
                               "shown again — re-enter it to change."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def test_connection(request, pk):
    """Live credential check so users see 'YouTube authorization expired' style messages (§39)."""
    conn = ConnectedPlatform.objects.filter(pk=pk, user=request.user).first()
    if not conn:
        return Response({"detail": "Connection not found."}, status=status.HTTP_404_NOT_FOUND)
    adapter = build_adapter(conn)
    try:
        page = adapter.fetch_comments(conn.external_account_id, None)
        return Response({"ok": True, "sample_count": len(page.comments)})
    except Exception as exc:
        detail = getattr(exc, "detail", None)
        msg = detail.get("detail") if isinstance(detail, dict) else str(exc)
        conn.status = "expired" if "expired" in str(msg).lower() else "error"
        conn.last_error = str(msg)[:500]
        conn.save(update_fields=["status", "last_error", "updated_at"])
        return Response({"ok": False, "message": str(msg)}, status=status.HTTP_502_BAD_GATEWAY)


urlpatterns = [
    path("", platform_catalog),
    path("connect/<str:platform>", connect_begin),
    path("callback/<str:platform>", callback),
    path("<uuid:pk>", disconnect_platform),
    path("<uuid:pk>/stream-key", set_manual_stream_key),
    path("<uuid:pk>/test", test_connection),
]
