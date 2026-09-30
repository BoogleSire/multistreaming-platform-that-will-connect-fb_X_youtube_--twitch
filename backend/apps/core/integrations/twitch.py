"""Twitch integration — official Twitch APIs only (§2).

Uses: OAuth 2.0 Authorization Code + PKCE, Helix API (send/receive channel chat messages,
moderation), EventSub webhooks (signature-verified with the standard Twitch HMAC-SHA256 over
ID+TIMESTAMP+BODY scheme).

Honest notes surfaced to users:
* Twitch does NOT expose stream keys through its public API. The user pastes their key once in
  Settings → Connected Platforms; the UI says so explicitly instead of hiding it.
* Chat posting requires `chat:edit` scope; message deletion requires moderator capability.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets
from typing import Optional
from urllib.parse import urlencode

import requests
from django.conf import settings

from .base import (
    Capability, CommentPage, IngestTarget, PlatformIntegration, UnifiedComment, register,
)
from apps.core.exceptions import PlatformError, TokenExpiredError

HELIX = "https://api.twitch.tv/helix"
AUTH_URL = "https://id.twitch.tv/oauth2/authorize"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@register
class TwitchIntegration(PlatformIntegration):
    platform_key = "twitch"
    display_name = "Twitch"
    settings_prefix = "TWITCH"
    supported_capabilities = frozenset({
        Capability.OAUTH, Capability.CHAT_READ, Capability.CHAT_POST,
        Capability.MODERATION_DELETE, Capability.MODERATION_BLOCK,
        Capability.VIEWER_STATS, Capability.WEBHOOK_COMMENTS, Capability.NEW_VIEWER_EVENTS,
    })

    # ------------------------------------------------------------ OAuth (PKCE)
    def authorization_url(self, state: str, redirect_uri: str) -> str:
        verifier = secrets.token_urlsafe(48)
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        self.credentials["_pkce_verifier"] = verifier  # persisted with the encrypted token row
        params = {
            "client_id": settings.TWITCH_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join([
                "chat:read", "chat:edit",                        # read & send channel chat
                "channel:read:subscriptions",                    # subscriber status for badges
                "moderator:manage:chat_messages",                # delete chat (where mod)
                "user:read:broadcast",                           # live status
            ]),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "force_verify": "true",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        resp = requests.post(TOKEN_URL, data={
            "client_id": settings.TWITCH_CLIENT_ID,
            "client_secret": settings.TWITCH_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": self.credentials.get("_pkce_verifier", ""),
        }, timeout=15)
        if resp.status_code != 200:
            raise PlatformError("Twitch authorization could not be completed. Please retry.",
                                platform="twitch", hint=resp.json().get("error_description", ""))
        tok = resp.json()
        me = requests.get(f"{HELIX}/users", headers=self._headers(tok["access_token"]),
                          timeout=15).json()
        user = (me.get("data") or [{}])[0]
        return {
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token"),
            "expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                seconds=tok.get("expires_in", 1_000_000)),
            "scope": " ".join(tok.get("scope") or []),
            "account": {
                "id": user.get("id"),
                "name": user.get("display_name") or user.get("login"),
                "avatar_url": user.get("profile_image_url"),
                "external_id": user.get("id"),
            },
        }

    def refresh_token(self, refresh_token: str) -> dict:
        resp = requests.post(TOKEN_URL, data={
            "client_id": settings.TWITCH_CLIENT_ID,
            "client_secret": settings.TWITCH_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, timeout=15)
        if resp.status_code == 401:
            raise TokenExpiredError("twitch")
        if resp.status_code != 200:
            raise PlatformError("Could not refresh the Twitch token; will retry shortly.",
                                platform="twitch")
        tok = resp.json()
        return {"access_token": tok["access_token"],
                "expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                    seconds=tok.get("expires_in", 1_000_000))}

    # ------------------------------------------------------------ helpers
    def _headers(self, token: Optional[str] = None) -> dict:
        return {
            "Authorization": f"Bearer {token or self.credentials['access_token']}",
            "Client-Id": settings.TWITCH_CLIENT_ID,
        }

    @property
    def _token(self) -> str:
        return self.credentials["access_token"]

    def _broadcaster_id(self) -> str:
        return self.credentials.get("account_external_id") or self.credentials.get("user_id", "")

    # ------------------------------------------------------------ comments
    def fetch_comments(self, external_stream_id: str, cursor: Optional[str]) -> CommentPage:
        """Backfill/poll via recent-messages (where entitled). Real-time delivery is handled by
        the EventSub webhook path and/or the persistent IRC-over-WSS listener service
        (apps.platforms.services.twitch_chat_listener) which feeds the same ingestion queue."""
        params = {"broadcaster_id": self._broadcaster_id(),
                  "user_id": self._broadcaster_id()}
        resp = requests.get(f"{HELIX}/moderation/chat",
                            params={**params, "limit": 120, **({"cursor": cursor} if cursor else {})},
                            headers=self._headers(), timeout=15)
        if resp.status_code == 401:
            raise TokenExpiredError("twitch")
        if resp.status_code == 403:
            raise PlatformError(
                "Twitch rejected the chat read — your token needs chat:read and moderator "
                "status in your own channel. Reconnect the Twitch account to approve scopes.",
                platform="twitch", retryable=False)
        if resp.status_code >= 400:
            raise PlatformError("Twitch chat could not be fetched right now.", platform="twitch")
        data = resp.json()
        out = []
        for item in data.get("data", []):
            out.append(UnifiedComment(
                platform="twitch",
                platform_comment_id=item.get("message_id", ""),
                author_name=item.get("username") or "viewer",
                author_id=item.get("user_id") or "",
                author_avatar_url=None,   # not exposed by this endpoint — shown as initials
                text=item.get("text", ""),
                published_at=item.get("timestamp") or "",
                viewer_status={},
                raw=item,
            ))
        return CommentPage(comments=out, next_cursor=(data.get("pagination") or {}).get("cursor"))

    def post_comment(self, external_stream_id: str, text: str,
                     reply_to_comment_id: Optional[str] = None) -> str:
        body = {
            "broadcaster_id": self._broadcaster_id(),
            "sender_id": self._broadcaster_id(),
            "message": text[:500],
        }
        if reply_to_comment_id:
            body["reply_parent_message_id"] = reply_to_comment_id
        resp = requests.post(f"{HELIX}/chat/messages", json=body,
                             headers={**self._headers(), "Content-Type": "application/json"},
                             timeout=15)
        if resp.status_code == 401:
            raise TokenExpiredError("twitch")
        if resp.status_code >= 400:
            err = (resp.json().get("error") or "").lower()
            raise PlatformError(
                "Twitch rejected this reply. Check chat rate limits or slow-mode on your "
                "channel." + (f" ({err})" if err else ""),
                platform="twitch", retryable="rate" in err)
        return (resp.json().get("data") or [{}])[0].get("message_id", "")

    # ------------------------------------------------------------ streaming (§F)
    def get_ingest_target(self) -> IngestTarget:
        manual_key = self.credentials.get("manual_stream_key")
        if not manual_key:
            raise PlatformError(
                "Twitch does not provide stream keys through its public API. Paste your key "
                "once in Settings → Connected Platforms → Twitch (find it in Twitch Creator "
                "Dashboard → Settings → Stream). Everything else stays automatic.",
                platform="twitch", retryable=False)
        primary = self.credentials.get("ingest_server") or "auto.rtmp.ninja.twitch.tv"
        return IngestTarget(rtmp_url=f"rtmp://{primary}/app/", stream_key=manual_key,
                            label="Twitch", note="User-provided key; server chosen automatically.")

    # ------------------------------------------------------------ moderation
    def delete_comment(self, external_comment_id: str) -> bool:
        resp = requests.delete(
            f"{HELIX}/moderation/chat",
            params={"broadcaster_id": self._broadcaster_id(), "message_id": external_comment_id},
            headers=self._headers(), timeout=15)
        return resp.status_code == 204

    def block_user(self, external_user_id: str) -> bool:
        resp = requests.post(
            f"{HELIX}/moderation/bans",
            params={"broadcaster_id": self._broadcaster_id(),
                    "moderator_id": self._broadcaster_id()},
            json={"data": {"user_id": external_user_id, "duration": 600,
                           "reason": "StreamForge moderation"}},
            headers={**self._headers(), "Content-Type": "application/json"}, timeout=15)
        return resp.status_code == 200

    # ------------------------------------------------------------ stats
    def get_viewer_stats(self, external_stream_id: str) -> dict:
        resp = requests.get(f"{HELIX}/streams", params={"user_id": self._broadcaster_id()},
                            headers=self._headers(), timeout=15)
        try:
            data = resp.json().get("data") or []
        except Exception:
            return {}
        if not data:
            return {}
        return {"concurrent_viewers": int(data[0].get("viewer_count") or 0),
                "source": "twitch_helix_streams"}

    # ------------------------------------------------------------ webhook (§32)
    def validate_webhook(self, headers: dict, body: bytes) -> bool:
        """EventSub signature: base64(HMAC-SHA256(secret, ID + TIMESTAMP + BODY))."""
        secret = settings.WEBHOOK_SECRET
        msg_id = headers.get("Twitch-Eventsub-Message-Id", "")
        ts = headers.get("Twitch-Eventsub-Message-Timestamp", "")
        sig_header = headers.get("Twitch-Eventsub-Message-Signature-V1", "")
        if not (secret and msg_id and ts and sig_header):
            return False
        try:
            when = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
            if abs((dt.datetime.now(dt.timezone.utc) - when).total_seconds()) > 600:
                return False  # reject stale/replayed deliveries
        except ValueError:
            return False
        digest = hmac.new(secret.encode(), (msg_id + ts).encode() + body, hashlib.sha256).digest()
        return hmac.compare_digest("v1=" + base64.b64encode(digest).decode(), sig_header)

    def normalize_webhook_comment(self, payload: dict) -> Optional[UnifiedComment]:
        sub = payload.get("subscription", {})
        if sub.get("type") != "channel.chat.message":
            return None
        ev = payload.get("event", {})
        return UnifiedComment(
            platform="twitch",
            platform_comment_id=ev.get("message_id", ""),
            author_name=ev.get("chatter_user_name") or "viewer",
            author_id=ev.get("chatter_user_id") or "",
            author_avatar_url=None,
            text=(ev.get("message") or {}).get("text", ""),
            published_at=ev.get("timestamp") or "",
            viewer_status={},
            raw=payload,
        )
