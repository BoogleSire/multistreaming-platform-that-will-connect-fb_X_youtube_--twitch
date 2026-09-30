"""TikTok integration — official TikTok for Developers APIs where they permit (§2).

Reality check encoded here (requirement §46 — no fake integrations):
* TikTok's public "Display API" exposes profile/video metadata only.
* LIVE access (live.chat, live.video) is gated behind the **TikTok LIVE Partner** program;
  chat read/write and stream keys require approved partner client credentials.
If TIKTOK_CLIENT_ID/SECRET are absent or the account lacks LIVE scope, every method raises a
CapabilityNotSupportedError naming exactly what approval is required — nothing is simulated.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional
from urllib.parse import urlencode

import requests
from django.conf import settings

from .base import (
    Capability, CommentPage, IngestTarget, PlatformIntegration, UnifiedComment, register,
)
from apps.core.exceptions import CapabilityNotSupportedError, PlatformError, TokenExpiredError

OPEN_API = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = f"{OPEN_API}/oauth/token/"

LIVE_PARTNER_NOTE = (
    "TikTok LIVE data (chat, stream key, viewer events) requires an approved TikTok LIVE "
    "partner developer app with the 'video.live' scope. Apply at developers.tiktok.com → "
    "LIVE for Developers, then set TIKTOK_CLIENT_ID / TIKTOK_CLIENT_SECRET."
)


@register
class TikTokIntegration(PlatformIntegration):
    platform_key = "tiktok"
    display_name = "TikTok"
    settings_prefix = "TIKTOK"
    # Only OAuth + basic profile are publicly guaranteed today.
    supported_capabilities = frozenset({Capability.OAUTH})

    def _partner_approved(self) -> bool:
        return self.credentials.get("has_live_scope", False)

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        params = {
            "client_key": settings.TIKTOK_CLIENT_ID,
            "response_type": "code",
            "scope": "user.info.profile" + (",video.live" if settings.TIKTOK_CLIENT_ID else ""),
            "state": state,
            "redirect_uri": redirect_uri,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        resp = requests.post(TOKEN_URL, data={
            "client_key": settings.TIKTOK_CLIENT_ID,
            "client_secret": settings.TIKTOK_CLIENT_SECRET,
            "code": code, "grant_type": "authorization_code"}, timeout=15)
        data = resp.json()
        tok = data.get("data", {})
        if "access_token" not in tok:
            desc = ((tok.get("error") or {}).get("description")) or ""
            raise PlatformError(
                "TikTok authorization could not be completed. If your developer app hasn't "
                "passed review, only test accounts can connect.", platform="tiktok", hint=desc)
        me = requests.get(f"{OPEN_API}/user/info/", params={"fields": "open_id,display_name,avatar_url"},
                          headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=15).json()
        u = (me.get("data") or {}).get("user", {})
        scopes = (tok.get("scope") or "")
        return {
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token"),
            "expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=tok.get("expires_in", 86400)),
            "scope": scopes,
            "account": {"id": u.get("open_id"), "name": u.get("display_name"),
                        "avatar_url": u.get("avatar_url"), "external_id": u.get("open_id")},
            "extra": {"has_live_scope": "video.live" in scopes},
        }

    def refresh_token(self, refresh_token: str) -> dict:
        resp = requests.post(TOKEN_URL, data={
            "client_key": settings.TIKTOK_CLIENT_ID,
            "client_secret": settings.TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token", "refresh_token": refresh_token}, timeout=15)
        tok = resp.json().get("data", {})
        if "access_token" not in tok:
            raise TokenExpiredError("tiktok")
        return {"access_token": tok["access_token"],
                "expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=tok.get("expires_in", 86400))}

    # ------------------------------------------------------------ gated features
    def fetch_comments(self, external_room_id: str, cursor: Optional[str]) -> CommentPage:
        if not self._partner_approved():
            raise CapabilityNotSupportedError("tiktok", "live chat reading", LIVE_PARTNER_NOTE)
        resp = requests.get(f"{OPEN_API}/video/live/chat/get/",
                            params={"room_id": external_room_id, "count": 50},
                            headers={"Authorization": f"Bearer {self.credentials['access_token']}"},
                            timeout=15)
        data = resp.json()
        chats = ((data.get("data") or {}).get("chats")) or []
        out = [UnifiedComment(
            platform="tiktok",
            platform_comment_id=c.get("message_id", ""),
            author_name=(c.get("user") or {}).get("nickname") or "viewer",
            author_id=(c.get("user") or {}).get("open_id", ""),
            author_avatar_url=(c.get("user") or {}).get("avatar_url"),
            text=c.get("content", ""),
            published_at=dt.datetime.fromtimestamp(int(c.get("timestamp", 0)) / 1000,
                                                   dt.timezone.utc).isoformat(),
            viewer_status={}, raw=c) for c in chats]
        return CommentPage(comments=out, next_cursor=(data.get("data") or {}).get("next_cursor"))

    def post_comment(self, external_room_id: str, text: str,
                     reply_to_comment_id: Optional[str] = None) -> str:
        if not self._partner_approved():
            raise CapabilityNotSupportedError("tiktok", "live chat replies", LIVE_PARTNER_NOTE)
        resp = requests.post(f"{OPEN_API}/video/live/chat/send/", json={
            "room_id": external_room_id, "content": text[:300]},
            headers={"Authorization": f"Bearer {self.credentials['access_token']}",
                     "Content-Type": "application/json"}, timeout=15)
        data = resp.json()
        status = ((data.get("data") or {}).get("status")) or {}
        if status != "SUCCESS":
            raise PlatformError("TikTok rejected this reply.", platform="tiktok",
                                hint=str(data.get("error")))
        return (data.get("data") or {}).get("message_id", "")

    def get_ingest_target(self) -> IngestTarget:
        if not self._partner_approved():
            raise CapabilityNotSupportedError("tiktok", "live broadcast ingest", LIVE_PARTNER_NOTE)
        resp = requests.post(f"{OPEN_API}/video/live/create/", json={"title": "StreamForge live"},
                             headers={"Authorization": f"Bearer {self.credentials['access_token']}",
                                      "Content-Type": "application/json"}, timeout=15)
        d = (resp.json().get("data") or {})
        if not d.get("rtmp_url"):
            raise PlatformError("TikTok did not create a live room for this account.",
                                platform="tiktok", retryable=False)
        return IngestTarget(rtmp_url=d["rtmp_url"], stream_key=d.get("rtmp_key", ""),
                            label="TikTok LIVE", note="Room created via official LIVE Partner API.")
