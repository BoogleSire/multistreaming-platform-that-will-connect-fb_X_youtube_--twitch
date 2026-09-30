"""YouTube integration — official Google APIs only (§2).

Uses: YouTube Data API v3 (liveBroadcasts, liveChatMessages), OAuth 2.0 with refresh tokens,
PubSubHubbub webhooks for live broadcast status changes.

Honest notes surfaced to users:
* Live chat must be ENABLED on the video (channel verification requirement) — errors say so.
* Stream keys come from CreateBroadcast/insert upstream — fully supported via API.
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
from apps.core.exceptions import PlatformError, TokenExpiredError

API = "https://www.googleapis.com/youtube/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


@register
class YouTubeIntegration(PlatformIntegration):
    platform_key = "youtube"
    display_name = "YouTube"
    settings_prefix = "YOUTUBE"
    supported_capabilities = frozenset({
        Capability.OAUTH, Capability.CHAT_READ, Capability.CHAT_POST,
        Capability.LIVE_BROADCAST, Capability.STREAM_KEY_API, Capability.VIEWER_STATS,
        Capability.MODERATION_DELETE, Capability.WEBHOOK_COMMENTS,
    })

    # ------------------------------------------------------------ OAuth
    def authorization_url(self, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": settings.YOUTUBE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join([
                "https://www.googleapis.com/auth/youtube",
                "https://www.googleapis.com/auth/youtube.force-ssl",  # needed to post chat
            ]),
            "access_type": "offline",           # we need refresh tokens
            "prompt": "consent",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        resp = requests.post(TOKEN_URL, data={
            "client_id": settings.YOUTUBE_CLIENT_ID,
            "client_secret": settings.YOUTUBE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }, timeout=15)
        if resp.status_code != 200:
            raise PlatformError("YouTube authorization could not be completed. Please retry.",
                                platform="youtube", hint=resp.json().get("error_description", ""))
        tok = resp.json()
        me = self._get("/channels", {"part": "snippet", "mine": "true"}, tok["access_token"])
        item = (me.get("items") or [{}])[0]
        return {
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token"),
            "expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=tok.get("expires_in", 3600)),
            "scope": tok.get("scope", ""),
            "account": {
                "id": item.get("id"),
                "name": item.get("snippet", {}).get("title"),
                "avatar_url": (item.get("snippet", {}).get("thumbnails", {})
                               .get("default", {}).get("url")),
                "external_id": item.get("id"),
            },
        }

    def refresh_token(self, refresh_token: str) -> dict:
        resp = requests.post(TOKEN_URL, data={
            "client_id": settings.YOUTUBE_CLIENT_ID,
            "client_secret": settings.YOUTUBE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }, timeout=15)
        if resp.status_code == 400 and resp.json().get("error") in ("invalid_grant", "revoked"):
            raise TokenExpiredError("youtube")
        if resp.status_code != 200:
            raise PlatformError("Could not refresh the YouTube token; will retry shortly.",
                                platform="youtube")
        tok = resp.json()
        return {"access_token": tok["access_token"],
                "expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=tok.get("expires_in", 3600))}

    # ------------------------------------------------------------ HTTP helper
    def _get(self, path: str, params: dict, access_token: str) -> dict:
        resp = requests.get(f"{API}{path}", params=params,
                            headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        self._raise_for(resp)
        return resp.json()

    def _post(self, path: str, params: dict, access_token: str, body: dict | None = None) -> dict:
        resp = requests.post(f"{API}{path}", params=params, json=body,
                             headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        self._raise_for(resp)
        return resp.json() if resp.content else {}

    def _raise_for(self, resp: requests.Response):
        if resp.status_code in (401, 403):
            try:
                reason = resp.json()["error"]["errors"][0]["reason"]
            except Exception:
                reason = ""
            if resp.status_code == 401 or reason == "authExpired":
                raise TokenExpiredError("youtube")
            if reason == "liveChatNotEnabledForVideo":
                raise PlatformError(
                    "Live chat is disabled on this YouTube video. Enable it in YouTube Studio → "
                    "Content → your video → Show more → Live chat, then retry. (Channel "
                    "verification may be required by YouTube.)",
                    platform="youtube", retryable=False,
                )
            raise PlatformError("YouTube rejected this action.", platform="youtube",
                                hint=reason, retryable=False)
        if resp.status_code >= 500:
            raise PlatformError("YouTube is temporarily unavailable. We'll keep retrying.",
                                platform="youtube")
        if resp.status_code >= 400:
            raise PlatformError(f"YouTube returned an error ({resp.status_code}).",
                                platform="youtube", retryable=False)

    @property
    def _token(self) -> str:
        return self.credentials["access_token"]

    # ------------------------------------------------------------ comments
    def _chat_id(self, external_video_id: str) -> str:
        meta = self._get("/videos", {"id": external_video_id, "part": "snippet,liveStreamingDetails"},
                        self._token)
        items = meta.get("items") or []
        if not items:
            raise PlatformError("That YouTube live video was not found via the API.",
                                platform="youtube", retryable=False)
        chat = items[0].get("liveStreamingDetails", {}).get("activeLiveChatId")
        if not chat:
            raise PlatformError(
                "No active live chat found. The stream must be live (or scheduled within 48h) "
                "and live chat enabled.", platform="youtube", retryable=False)
        return chat

    def fetch_comments(self, external_stream_id: str, cursor: Optional[str]) -> CommentPage:
        chat_id = self._chat_id(external_stream_id)
        params = {"part": "snippet,authorDetails", "liveChatId": chat_id,
                  "maxResults": 200, "tc": dt.datetime.now(dt.timezone.utc).isoformat()}
        if cursor:
            params["pageToken"] = cursor
        data = self._get("/liveChat/messages", params, self._token)
        out = []
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            ad = item.get("authorDetails", {})
            txt = sn.get("textMessage", {}).get("messageText") or sn.get("superChatDetails", {}).get("commentText")
            if not txt:
                continue
            out.append(UnifiedComment(
                platform="youtube",
                platform_comment_id=item["id"],
                author_name=ad.get("channelTitle") or ad.get("displayName") or "viewer",
                author_id=ad.get("channelId") or ad.get("userId") or "",
                author_avatar_url=(ad.get("profileImageUrl")),
                text=txt,
                published_at=sn.get("publishedAt") or "",
                viewer_status={
                    "is_subscriber": ad.get("isSubscriber", False),
                    "is_member": ad.get("isMembership", False),
                    "is_mod": ad.get("isChannelOwner", False) or "moderator" in (ad.get("authorBadges") or [{}])[0].get("badgeType", "").lower(),
                    "super_chat_amount": sn.get("superChatDetails", {}).get("displayString"),
                },
                raw=item,
            ))
        return CommentPage(comments=out, next_cursor=data.get("nextPageToken"))

    def post_comment(self, external_stream_id: str, text: str,
                     reply_to_comment_id: Optional[str] = None) -> str:
        chat_id = self._chat_id(external_stream_id)
        body = {"snippet": {"type": "textMessageEvent",
                            "textMessageDetails": {"messageText": text[:2000]}}}
        if reply_to_comment_id:
            body["parentId"] = reply_to_comment_id
        res = self._post("/liveChat/messages", {"part": "snippet"}, self._token, body)
        return res["id"]

    # ------------------------------------------------------------ streaming (§F)
    def get_ingest_target(self) -> IngestTarget:
        """Create a scheduled live broadcast through the official API and read its ingest."""
        title = self.credentials.get("broadcast_title", "StreamForge live")
        created = self._post("/liveBroadcasts",
                             {"part": "snippet,status,contentDetails"},
                             self._token,
                             {"snippet": {"title": title, "description": self.credentials.get("broadcast_desc", "")},
                              "status": "ready"})
        sl = created["id"]
        streams = self._get("/liveStreams", {"part": "cdn", "mine": "true", "maxResults": 5},
                            self._token).get("items", [])
        cdn = (streams[0].get("cdn") if streams else None) or \
              created.get("contentDetails", {}).get("stream") or {}
        key = cdn.get("ingestionInfo", {}).get("streamName")
        base = cdn.get("ingestionInfo", {}).get("ingestionAddress") or "rtmp://a.rtmp.youtube.com/live2"
        if not key:
            raise PlatformError(
                "YouTube did not return an ingest key for this account. Ensure 'Go live' "
                "eligibility on your channel, then reconnect.", platform="youtube", retryable=False)
        self.credentials["external_broadcast_id"] = sl
        return IngestTarget(rtmp_url=base, stream_key=key, label=f"YouTube · {title}",
                            note="Ingest obtained via official YouTube Live API.")

    def start_stream(self, external_broadcast_id: str):
        self._post(f"/liveBroadcasts/{external_broadcast_id}/transition",
                   {"broadcastStatus": "live", "part": "snippet,status"}, self._token)

    def stop_stream(self, external_broadcast_id: str):
        self._post(f"/liveBroadcasts/{external_broadcast_id}/transition",
                   {"broadcastStatus": "complete", "part": "snippet,status"}, self._token)

    # ------------------------------------------------------------ moderation
    def delete_comment(self, external_comment_id: str) -> bool:
        try:
            requests.delete(f"{API}/liveChat/messages/{external_comment_id}",
                            headers={"Authorization": f"Bearer {self._token}"}, timeout=15)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ stats
    def get_viewer_stats(self, external_stream_id: str) -> dict:
        data = self._get("/videos", {"id": external_stream_id, "part": "statistics,liveStreamingDetails"},
                        self._token)
        items = data.get("items") or []
        if not items:
            return {}
        st = items[0].get("statistics", {})
        ls = items[0].get("liveStreamingDetails", {})
        return {
            "concurrent_viewers": int(ls.get("concurrentViewers") or 0),
            "total_views": int(st.get("viewCount") or 0),
            "likes": int(st.get("likeCount") or 0),
            "source": "youtube_data_api_v3",
        }

    # ------------------------------------------------------------ webhook (§32)
    def validate_webhook(self, headers: dict, body: bytes) -> bool:
        """Google PubSubHubbub: verify HMAC-SHA1 signature of X-Hub-Signature."""
        import hashlib
        import hmac

        secret = settings.WEBHOOK_SECRET
        sig_header = headers.get("X-Hub-Signature", "")
        if not secret or not sig_header.startswith("sha1="):
            return False
        expected = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
        return hmac.compare_digest(expected, sig_header)
