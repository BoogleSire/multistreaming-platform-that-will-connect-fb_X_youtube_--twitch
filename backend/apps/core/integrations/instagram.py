"""Instagram integration — official Instagram Graph API where it permits (§2).

Reality check encoded here (§46):
* The public Instagram API supports comments on **business-account media**, but there is NO
  public third-party live-broadcast ingest and NO realtime live-comment API for IG Live from
  third-party apps. StreamForge therefore:
   - connects Instagram business accounts via Facebook Login (Graph API),
   - collects comments on posts/reels into the unified feed,
   - refuses to fake IG live streaming and says so plainly.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional
from urllib.parse import urlencode

import requests
from django.conf import settings

from .base import (
    Capability, CommentPage, PlatformIntegration, UnifiedComment, register,
)
from apps.core.exceptions import CapabilityNotSupportedError, PlatformError, TokenExpiredError

GRAPH = "https://graph.facebook.com/v21.0"

IG_LIVE_NOTE = (
    "Instagram does not expose third-party live-video ingestion or a realtime live-chat API. "
    "Closest officially supported behavior: StreamForge monitors comments on your IG posts & "
    "Reels through the Instagram Graph API. For live engagement, broadcast to another "
    "connected platform and cross-post the announcement."
)


@register
class InstagramIntegration(PlatformIntegration):
    platform_key = "instagram"
    display_name = "Instagram"
    settings_prefix = "INSTAGRAM"
    supported_capabilities = frozenset({
        Capability.OAUTH, Capability.CHAT_READ, Capability.CHAT_POST, Capability.MODERATION_DELETE,
    })

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        # Instagram Graph API rides on Facebook Login.
        params = {
            "client_id": settings.INSTAGRAM_CLIENT_ID or settings.FACEBOOK_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": "instagram_basic,instagram_manage_comments,instagram_manage_insights,pages_show_list",
        }
        return f"https://www.facebook.com/v21.0/dialog/oauth?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        secret = settings.INSTAGRAM_CLIENT_SECRET or settings.FACEBOOK_CLIENT_SECRET
        client = settings.INSTAGRAM_CLIENT_ID or settings.FACEBOOK_CLIENT_ID
        short = requests.get(f"{GRAPH}/oauth/access_token", params={
            "client_id": client, "client_secret": secret, "code": code,
            "redirect_uri": redirect_uri}, timeout=15).json()
        if "access_token" not in short:
            raise PlatformError("Instagram login could not be completed. Ensure the Instagram "
                                "account is a Business/Creator account linked to a Page.",
                                platform="instagram")
        long = requests.get(f"{GRAPH}/oauth/access_token", params={
            "grant_type": "fb_exchange_token", "client_id": client, "client_secret": secret,
            "fb_exchange_token": short["access_token"]}, timeout=15).json()
        token = long.get("access_token", short["access_token"])
        pages = requests.get(f"{GRAPH}/me/accounts", params={"access_token": token}, timeout=15).json().get("data", [])
        ig_account = None
        page_token = None
        for p in pages:
            ig = requests.get(f"{GRAPH}/{p['id']}", params={
                "fields": "instagram_business_account{id,username,profile_picture_url}",
                "access_token": p["access_token"]}, timeout=15).json()
            if ig.get("instagram_business_account"):
                ig_account = ig["instagram_business_account"]
                page_token = p["access_token"]
                break
        if not ig_account:
            raise PlatformError(
                "No Instagram Business account found linked to your Pages. Convert the account "
                "to Business/Creator and link it to a Facebook Page, then reconnect.",
                platform="instagram", retryable=False)
        return {
            "access_token": page_token,
            "expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=59),
            "scope": "instagram_basic,instagram_manage_comments",
            "account": {"id": ig_account["id"], "name": ig_account.get("username"),
                        "avatar_url": ig_account.get("profile_picture_url"),
                        "external_id": ig_account["id"]},
        }

    @property
    def _ig_user(self) -> str:
        return self.credentials["account_external_id"]

    def fetch_comments(self, external_media_id: str, cursor: Optional[str]) -> CommentPage:
        url = cursor or f"{GRAPH}/{external_media_id}/comments"
        resp = requests.get(url, params={
            "fields": "id,text,timestamp,like_count,from{id,username,profile_picture_url}",
            "limit": 50, "access_token": self.credentials["access_token"]}, timeout=15)
        data = resp.json()
        if "error" in data:
            if data["error"].get("code") == 190:
                raise TokenExpiredError("instagram")
            raise PlatformError("Instagram comments could not be fetched right now.",
                                platform="instagram")
        out = []
        for item in data.get("data", []):
            frm = item.get("from", {})
            out.append(UnifiedComment(
                platform="instagram",
                platform_comment_id=item.get("id", ""),
                author_name=frm.get("username") or "instagram user",
                author_id=frm.get("id") or "",
                author_avatar_url=frm.get("profile_picture_url"),
                text=item.get("text", ""),
                published_at=item.get("timestamp") or "",
                viewer_status={"likes": int(item.get("like_count") or 0)},
                raw=item,
            ))
        return CommentPage(comments=out, next_cursor=(data.get("paging") or {}).get("after")
                           and f"{GRAPH}/{external_media_id}/comments?access_token={self.credentials['access_token']}&after={data['paging']['after']}")

    def post_comment(self, external_media_id: str, text: str,
                     reply_to_comment_id: Optional[str] = None) -> str:
        target = reply_to_comment_id or external_media_id
        resp = requests.post(f"{GRAPH}/{target}/comments",
                             data={"message": text[:2000], "access_token": self.credentials["access_token"]},
                             timeout=15)
        data = resp.json()
        if "error" in data:
            raise PlatformError("Instagram rejected this reply. The session may have expired — "
                                "reconnect your Instagram account.", platform="instagram",
                                retryable=False)
        return data.get("id", "")

    def get_ingest_target(self):
        raise CapabilityNotSupportedError("instagram", "live video broadcasting", IG_LIVE_NOTE)

    def delete_comment(self, external_comment_id: str) -> bool:
        resp = requests.delete(f"{GRAPH}/{external_comment_id}",
                               params={"access_token": self.credentials["access_token"]}, timeout=15)
        return resp.json().get("success", False) is True
