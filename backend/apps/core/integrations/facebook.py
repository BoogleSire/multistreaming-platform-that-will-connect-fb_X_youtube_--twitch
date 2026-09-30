"""Facebook integration — official Meta Graph API / Facebook Login only (§2).

Uses: OAuth 2.0 (facebook.com/v21.0/dialog/oauth + access_token exchange), Graph API for
Page videos & live comments, Page webhook subscriptions (X-Hub-Signature-256 verified).

Honest notes surfaced to users:
* Live comment reading on Facebook requires a **Page** (user-profile live video chat is not
  exposed by the public API). Users connect a Page; errors say so.
* App Review: `pages_manage_engagement`, `pages_read_engagement`, `pages_show_list`,
  `publish_video` scopes need Meta app review before serving public users. Until approved,
  only testers/roles of the app work — surfaced in setup docs (docs/PLATFORM_INTEGRATIONS.md).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from typing import Optional
from urllib.parse import urlencode

import requests
from django.conf import settings

from .base import (
    Capability, CommentPage, IngestTarget, PlatformIntegration, UnifiedComment, register,
)
from apps.core.exceptions import PlatformError, TokenExpiredError

GRAPH = "https://graph.facebook.com/v21.0"
AUTH_URL = "https://www.facebook.com/v21.0/dialog/oauth"
TOKEN_URL = f"{GRAPH}/oauth/access_token"


@register
class FacebookIntegration(PlatformIntegration):
    platform_key = "facebook"
    display_name = "Facebook"
    settings_prefix = "FACEBOOK"
    supported_capabilities = frozenset({
        Capability.OAUTH, Capability.CHAT_READ, Capability.CHAT_POST,
        Capability.LIVE_BROADCAST, Capability.VIEWER_STATS,
        Capability.MODERATION_DELETE, Capability.WEBHOOK_COMMENTS,
    })

    # ------------------------------------------------------------ OAuth
    def authorization_url(self, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": settings.FACEBOOK_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": ",".join([
                "pages_show_list", "pages_read_engagement",
                "pages_manage_engagement",   # comment on behalf of the page
                "publish_video",              # create live video via API
            ]),
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        # Step 1: short-lived user token
        short = requests.get(TOKEN_URL, params={
            "client_id": settings.FACEBOOK_CLIENT_ID,
            "client_secret": settings.FACEBOOK_CLIENT_SECRET,
            "code": code, "redirect_uri": redirect_uri}, timeout=15).json()
        if "access_token" not in short:
            raise PlatformError("Facebook authorization could not be completed. Please retry.",
                                platform="facebook", hint=(short.get("error") or {}).get("message", ""))
        # Step 2: long-lived token (60-day) — required for background workers
        long = requests.get(TOKEN_URL, params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.FACEBOOK_CLIENT_ID,
            "client_secret": settings.FACEBOOK_CLIENT_SECRET,
            "fb_exchange_token": short["access_token"]}, timeout=15).json()
        token = long.get("access_token", short["access_token"])
        expires = long.get("expires_in")
        # Step 3: pick the primary connected Page
        pages = requests.get(f"{GRAPH}/me/accounts", params={"access_token": token},
                             timeout=15).json().get("data", [])
        page = pages[0] if pages else None
        if not page:
            raise PlatformError(
                "No Facebook Page found on this account. StreamForge reads and replies to live "
                "comments through the official Page APIs — create or gain access to a Page, "
                "then reconnect.", platform="facebook", retryable=False)
        me = requests.get(f"{GRAPH}/me", params={"fields": "name,picture", "access_token": token},
                          timeout=15).json()
        return {
            "access_token": page["access_token"],          # PAGE token — what we actually use
            "user_access_token": token,
            "refresh_token": None,
            "expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=expires or 5_184_000),
            "scope": "page",
            "account": {
                "id": page["id"], "name": page["name"],
                "avatar_url": (me.get("picture", {}).get("data", {}).get("url")),
                "external_id": page["id"],
            },
        }

    @property
    def _token(self) -> str:
        return self.credentials["access_token"]

    # ------------------------------------------------------------ comments
    def fetch_comments(self, external_video_id: str, cursor: Optional[str]) -> CommentPage:
        url = cursor or f"{GRAPH}/{external_video_id}/comments"
        resp = requests.get(url, params={
            "fields": "id,text,from{name,picture{url}},created_time,user_likes,comment_count",
            "limit": 100, "access_token": self._token, "order": "chronological"}, timeout=15)
        data = resp.json()
        if resp.status_code in (401, 190):
            raise TokenExpiredError("facebook")
        if "error" in data:
            code = data["error"].get("code")
            if code == 10:
                raise PlatformError(
                    "This video isn't accessible with your current Page permissions. It must "
                    "be published by the connected Page.", platform="facebook", retryable=False)
            raise PlatformError("Facebook could not deliver comments right now; we'll retry.",
                                platform="facebook")
        out = []
        for item in data.get("data", []):
            frm = item.get("from", {})
            out.append(UnifiedComment(
                platform="facebook",
                platform_comment_id=item.get("id", ""),
                author_name=frm.get("name") or "Facebook user",
                author_id=frm.get("id") or "",
                author_avatar_url=((frm.get("picture") or {}).get("data") or {}).get("url"),
                text=item.get("text", ""),
                published_at=item.get("created_time") or "",
                viewer_status={"likes": int(item.get("user_likes") or 0)},
                raw=item,
            ))
        nxt = (data.get("paging") or {}).get("next")
        return CommentPage(comments=out, next_cursor=nxt)

    def post_comment(self, external_video_id: str, text: str,
                     reply_to_comment_id: Optional[str] = None) -> str:
        target = reply_to_comment_id or external_video_id
        resp = requests.post(f"{GRAPH}/{target}/comments",
                             data={"message": text[:4000], "access_token": self._token}, timeout=15)
        data = resp.json()
        if "error" in data:
            raise PlatformError(
                "Facebook rejected this reply. The Page token may have expired — reconnect "
                "your Facebook account in Settings → Connected Platforms.",
                platform="facebook", retryable=False)
        return data.get("id", "")

    # ------------------------------------------------------------ streaming (§F)
    def get_ingest_target(self) -> IngestTarget:
        """Create a live video on the Page via Graph API and read its RTMP upload URL."""
        title = self.credentials.get("broadcast_title", "StreamForge live")
        created = requests.post(f"{GRAPH}/{self.credentials['account_external_id']}/live_videos",
                                data={"title": title, "description": self.credentials.get("broadcast_desc", ""),
                                      "access_token": self._token}, timeout=15).json()
        if "error" in created:
            raise PlatformError(
                "Facebook could not create the live video. Ensure 'publish_video' has been "
                "approved for your app and the Page is allowed to go live.",
                platform="facebook", hint=(created.get("error") or {}).get("message", ""))
        key = created.get("stream_key")
        url = created.get("upload_url")
        if not (key and url):
            raise PlatformError(
                "Facebook did not return an ingest endpoint for this Page. Pages become "
                "eligible for API live publishing after standard access review.",
                platform="facebook", retryable=False)
        self.credentials["external_video_id"] = created.get("id")
        # upload_url is https://live-upload.facebook.com/DMV/<video_id>?vs=<sig>; the key is separate
        base = url.split("?")[0]
        return IngestTarget(rtmp_url=f"rtmp://live-upload.facebook.com/rtmp/{created['id']}",
                            stream_key=key, label=f"Facebook · {title}",
                            note="Ingest obtained via official Graph API live_videos endpoint.")

    def stop_stream(self, external_video_id: str):
        requests.post(f"{GRAPH}/{external_video_id}",
                      data={"status": "LIVE_STOPPED", "access_token": self._token}, timeout=15)

    # ------------------------------------------------------------ moderation
    def delete_comment(self, external_comment_id: str) -> bool:
        resp = requests.delete(f"{GRAPH}/{external_comment_id}",
                               params={"access_token": self._token}, timeout=15)
        return resp.json().get("success", False) is True

    # ------------------------------------------------------------ stats
    def get_viewer_stats(self, external_video_id: str) -> dict:
        data = requests.get(f"{GRAPH}/{external_video_id}/insights/live_views",
                            params={"access_token": self._token}, timeout=15).json()
        try:
            val = data["data"][0]["values"][0]["value"]
            return {"concurrent_viewers": int(val), "source": "fb_graph_insights"}
        except Exception:
            return {}

    # ------------------------------------------------------------ webhook (§32)
    def validate_webhook(self, headers: dict, body: bytes) -> bool:
        """Meta signs payloads: X-Hub-Signature-256 = sha256=HMAC(app_secret, body)."""
        secret = settings.FACEBOOK_CLIENT_SECRET
        sig = headers.get("X-Hub-Signature-256", "")
        if not secret or not sig.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    def normalize_webhook_comment(self, payload: dict) -> Optional[UnifiedComment]:
        entry = (payload.get("entry") or [{}])[0]
        changes = entry.get("changes") or []
        item = next((c for c in changes if c.get("field") == "feed"), None)
        if not item:
            return None
        v = item.get("value", {})
        if v.get("item") != "comment" or v.get("verb") not in ("add", "created"):
            return None
        frm = v.get("from", {})
        return UnifiedComment(
            platform="facebook",
            platform_comment_id=v.get("comment_id", ""),
            author_name=frm.get("name") or "Facebook user",
            author_id=frm.get("id") or "",
            author_avatar_url=None,   # webhook omits avatar; fetched lazily by processor
            text=v.get("message", ""),
            published_at=v.get("created_time") or "",
            viewer_status={},
            raw=payload,
        )
