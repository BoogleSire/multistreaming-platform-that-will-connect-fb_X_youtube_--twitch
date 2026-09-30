"""Integration adapter framework (§2, §46).

Every platform is an independently-maintainable adapter implementing PlatformIntegration.
Adapters talk ONLY to official APIs — no scraping, no ToS bypass. Where a public API does
not permit a feature, adapters declare it absent in `supported_capabilities` or raise
CapabilityNotSupportedError with the exact credential/approval needed.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Capability(str, Enum):
    OAUTH = "oauth"                          # official OAuth 2.0 connect flow
    CHAT_READ = "chat_read"                  # read live comments/chat (poll or webhook)
    CHAT_POST = "chat_post"                  # post replies as the channel owner/bot
    LIVE_BROADCAST = "live_broadcast"        # start/stop broadcast + obtain ingest via API
    STREAM_KEY_API = "stream_key_api"        # stream key retrievable programmatically
    MODERATION_DELETE = "moderation_delete"  # hide/delete comments on-platform
    MODERATION_BLOCK = "moderation_block"    # ban/block users on-platform
    VIEWER_STATS = "viewer_stats"            # concurrent viewers / view counts
    NEW_VIEWER_EVENTS = "new_viewer_events"  # join/arrive events for welcome bot
    WEBHOOK_COMMENTS = "webhook_comments"    # push-based comment ingestion


@dataclass
class UnifiedComment:
    """Normalized comment shape all adapters emit into the unified feed (§6)."""

    platform: str
    platform_comment_id: str
    author_name: str
    author_id: str
    author_avatar_url: Optional[str]         # only when the API exposes it
    text: str
    published_at: str                        # ISO-8601 UTC
    viewer_status: dict = field(default_factory=dict)   # e.g. {"is_subscriber": true}
    raw: dict = field(default_factory=dict)              # untouched payload for forensics
    simulated: bool = False                  # DEV-ONLY flag (§46) — never true in prod


@dataclass
class CommentPage:
    comments: list[UnifiedComment]
    next_cursor: Optional[str]               # poll token; webhook ingestion is cursorless


@dataclass
class IngestTarget:
    """Where/how to push our video to this platform."""

    rtmp_url: str
    stream_key: str
    label: str = ""
    note: str = ""                           # honest caveats, surfaced in UI


class PlatformIntegration(abc.ABC):
    """Base class every adapter extends. Registry-driven; add a file to add a platform."""

    platform_key: str = ""                   # "youtube", "twitch", ...
    display_name: str = ""
    settings_prefix: str = ""                # env-var prefix, e.g. "YOUTUBE"
    supported_capabilities: frozenset[Capability] = frozenset()

    def __init__(self, credentials: dict[str, Any]):
        """`credentials` contains client_id/secret (settings) and the decrypted user token."""
        self.credentials = credentials

    @classmethod
    def is_configured(cls) -> bool:
        from django.conf import settings

        return bool(getattr(settings, f"{cls.settings_prefix}_CLIENT_ID", "")
                    and getattr(settings, f"{cls.settings_prefix}_CLIENT_SECRET", ""))

    @classmethod
    def supports(cls, cap: Capability) -> bool:
        return cap in cls.supported_capabilities

    # ---- OAuth (§30: flows use official endpoints only) --------------
    @abc.abstractmethod
    def authorization_url(self, state: str, redirect_uri: str) -> str: ...

    @abc.abstractmethod
    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        """Return {access_token, refresh_token?, expires_at?, scope, account:{id,name,...}}."""

    def refresh_token(self, refresh_token: str) -> dict:
        raise NotImplementedError(f"{self.display_name} tokens do not refresh.")

    # ---- comments ----------------------------------------------------
    @abc.abstractmethod
    def fetch_comments(self, external_stream_id: str, cursor: Optional[str]) -> CommentPage: ...

    @abc.abstractmethod
    def post_comment(self, external_stream_id: str, text: str,
                     reply_to_comment_id: Optional[str] = None) -> str:
        """Return the new comment id on success; raise PlatformError otherwise."""

    # ---- streaming ---------------------------------------------------
    def get_ingest_target(self) -> IngestTarget:
        from apps.core.exceptions import CapabilityNotSupportedError

        raise CapabilityNotSupportedError(
            self.platform_key, "programmatic stream-key provisioning",
            "Provide your stream key manually in Settings → Platforms if this platform "
            "does not expose it through its public API.",
        )

    # ---- moderation (only where platform APIs permit) ----------------
    def delete_comment(self, external_comment_id: str) -> bool:
        return False

    def block_user(self, external_user_id: str) -> bool:
        return False

    # ---- stats -------------------------------------------------------
    def get_viewer_stats(self, external_stream_id: str) -> dict:
        return {}

    # ---- webhook verification (§32) ----------------------------------
    def validate_webhook(self, headers: dict, body: bytes) -> bool:
        return False

    def normalize_webhook_comment(self, payload: dict) -> Optional[UnifiedComment]:
        return None


# ---------------------------------------------------------------- registry
_REGISTRY: dict[str, type[PlatformIntegration]] = {}


def register(cls: type[PlatformIntegration]) -> type[PlatformIntegration]:
    _REGISTRY[cls.platform_key] = cls
    return cls


def get_integration_class(platform_key: str) -> type[PlatformIntegration]:
    try:
        return _REGISTRY[platform_key]
    except KeyError:
        from apps.core.exceptions import PlatformError

        raise PlatformError(
            f"Unknown platform '{platform_key}'. Supported: {', '.join(sorted(_REGISTRY))}.",
            retryable=False,
        )


def load_all_adapters():
    """Import every adapter module so registration happens at app boot."""
    from . import youtube, twitch, facebook, x, tiktok, instagram  # noqa: F401


def available_platforms() -> list[dict]:
    """Configured platforms + their capabilities — drives the UI's honest feature matrix."""
    out = []
    for key, cls in sorted(_REGISTRY.items()):
        out.append(
            {
                "key": key,
                "display_name": cls.display_name,
                "configured": cls.is_configured(),
                "capabilities": sorted(c.value for c in cls.supported_capabilities),
            }
        )
    return out
