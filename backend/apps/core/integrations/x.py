"""X (Twitter) integration — official X API v2 + OAuth 2.0 with PKCE only (§2).

Uses: OAuth 2.0 Authorization Code + PKCE, v2 tweets search/mentions for comment collection
(X has no "live chat" object — replies to the broadcast tweet ARE the comment stream),
media upload not required (video goes RTMP→ restream isn't natively supported by X for third
parties; instead we post the Go-Live URL/tweet).

Honest notes surfaced to users:
* X does not allow third-party apps to ingest arbitrary live video via a public API today.
  StreamForge therefore treats your X *post/replies* as the engagement surface and clearly
  marks live-broadcast on X as unavailable rather than faking it.
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
    Capability, CommentPage, PlatformIntegration, UnifiedComment, register,
)
from apps.core.exceptions import PlatformError, TokenExpiredError

API = "https://api.x.com/2"
AUTH_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"


@register
class XIntegration(PlatformIntegration):
    platform_key = "x"
    display_name = "X"
    settings_prefix = "X"
    supported_capabilities = frozenset({
        Capability.OAUTH, Capability.CHAT_READ, Capability.CHAT_POST,
        Capability.VIEWER_STATS,   # impression metrics where entitled
    })

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.credentials["_pkce_verifier"] = verifier
        params = {
            "client_id": settings.X_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "tweet.read tweet.write offline.access users.read",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        auth = base64.b64encode(f"{settings.X_CLIENT_ID}:{settings.X_CLIENT_SECRET}".encode()).decode()
        resp = requests.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "code_verifier": self.credentials.get("_pkce_verifier", "")},
            headers={"Authorization": f"Basic {auth}"}, timeout=15)
        if resp.status_code != 200:
            raise PlatformError("X authorization could not be completed. Please retry.",
                                platform="x", hint=resp.json().get("error_description", ""))
        tok = resp.json()
        me = requests.get(f"{API}/me", headers=self._headers(tok["access_token"]), timeout=15).json()
        user = me.get("data", {})
        return {
            "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token"),
            "expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=tok.get("expires_in", 7200)),
            "scope": tok.get("scope", ""),
            "account": {"id": user.get("id"), "name": user.get("username"),
                        "avatar_url": user.get("profile_image_url"), "external_id": user.get("id")},
        }

    def refresh_token(self, refresh_token: str) -> dict:
        auth = base64.b64encode(f"{settings.X_CLIENT_ID}:{settings.X_CLIENT_SECRET}".encode()).decode()
        resp = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                             headers={"Authorization": f"Basic {auth}"}, timeout=15)
        if resp.status_code == 400:
            raise TokenExpiredError("x")
        if resp.status_code != 200:
            raise PlatformError("Could not refresh the X token; will retry shortly.", platform="x")
        tok = resp.json()
        return {"access_token": tok["access_token"],
                "expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=tok.get("expires_in", 7200))}

    def _headers(self, token: Optional[str] = None) -> dict:
        return {"Authorization": f"Bearer {token or self.credentials['access_token']}"}

    # ------------------------------------------------------------ comments = replies to our live tweet
    def fetch_comments(self, external_tweet_id: str, cursor: Optional[str]) -> CommentPage:
        """All replies under the broadcast tweet are normalized into the unified feed."""
        params = {"max_results": 100,
                  "tweet.fields": "created_at,author_id",
                  "expansions": "author_id",
                  "user.fields": "name,profile_image_url"}
        if cursor:
            params["pagination_token"] = cursor
        resp = requests.get(f"{API}/tweets/{external_tweet_id}/replies", params=params,
                            headers=self._headers(), timeout=15)
        if resp.status_code == 401:
            raise TokenExpiredError("x")
        if resp.status_code == 403:
            raise PlatformError(
                "Your X developer tier doesn't include reply search for this endpoint. Upgrade "
                "the app's API plan or connect an account with access.", platform="x", retryable=False)
        if resp.status_code >= 400:
            raise PlatformError("X replies could not be fetched right now.", platform="x")
        data = resp.json()
        users = {u["id"]: u for u in (data.get("includes") or {}).get("users", [])}
        out = []
        for t in data.get("data", []):
            author = users.get(t.get("author_id"), {})
            out.append(UnifiedComment(
                platform="x",
                platform_comment_id=t["id"],
                author_name=author.get("name") or "@" + (author.get("username") or "user"),
                author_id=t.get("author_id") or "",
                author_avatar_url=author.get("profile_image_url"),
                text=t.get("text", ""),
                published_at=t.get("created_at") or "",
                viewer_status={},
                raw=t,
            ))
        return CommentPage(comments=out, next_cursor=(data.get("meta") or {}).get("next_token"))

    def post_comment(self, external_tweet_id: str, text: str,
                     reply_to_comment_id: Optional[str] = None) -> str:
        body = {"text": text[:280], "reply": {"in_reply_to_tweet_id": reply_to_comment_id or external_tweet_id}}
        resp = requests.post(f"{API}/tweets", json=body,
                             headers={**self._headers(), "Content-Type": "application/json"}, timeout=15)
        if resp.status_code == 401:
            raise TokenExpiredError("x")
        if resp.status_code >= 400:
            err = resp.json().get("errors", [{}])[0].get("message", "")
            raise PlatformError(f"X rejected this reply. {err}".strip(), platform="x",
                                retryable="rate limit" in err.lower())
        return resp.json().get("data", {}).get("id", "")

    def get_ingest_target(self):
        from apps.core.exceptions import CapabilityNotSupportedError

        raise CapabilityNotSupportedError(
            "x", "third-party live video ingestion",
            "X does not offer a public live-ingest API for third-party apps. Closest supported "
            "flow: go live on X directly (or via an approved partner encoder), then connect the "
            "broadcast tweet here so StreamForge can collect its replies into the unified feed.")

    def get_viewer_stats(self, external_tweet_id: str) -> dict:
        resp = requests.get(f"{API}/tweets/{external_tweet_id}",
                            params={"tweet.fields": "public_metrics"},
                            headers=self._headers(), timeout=15)
        try:
            pm = resp.json()["data"]["public_metrics"]
            return {"likes": int(pm.get("retweet_count") or 0),
                    "impressions": int(pm.get("impression_count") or 0),
                    "source": "x_api_v2_public_metrics"}
        except Exception:
            return {}

    def validate_webhook(self, headers: dict, body: bytes) -> bool:
        # X webhooks (account activity) use CRC + OAuth2 app-level bearer; handled at view level.
        return False
