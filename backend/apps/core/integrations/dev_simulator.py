"""DEV-ONLY simulated integration (§46).

This adapter exists so developers can exercise the comment pipeline without platform
credentials. It is DISABLED unless MOCK_PLATFORM_ENABLED=true, refuses to load in
production settings, and every message it produces carries simulated=True which the UI
renders with a visible "DEV SIM" badge. It never impersonates a real platform account.
"""
from __future__ import annotations

import datetime as dt
import random
import uuid
from typing import Optional

from django.conf import settings

from .base import Capability, CommentPage, PlatformIntegration, UnifiedComment, register


@register
class DevSimulatorIntegration(PlatformIntegration):
    platform_key = "dev_simulator"
    display_name = "DEV Simulator (mock)"
    settings_prefix = "DEV_SIMULATOR"
    supported_capabilities = frozenset({Capability.CHAT_READ})

    _SAMPLES = [
        ("Alex", "Testing the pipeline — this text is generated locally, not from any platform."),
        ("Priya", "Second simulated ping for rate-limit tests."),
        ("Sam", "Does dedupe work? (sim)"),
    ]

    @classmethod
    def is_configured(cls) -> bool:  # only when explicitly enabled
        return bool(getattr(settings, "MOCK_PLATFORM_ENABLED", False))

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        raise RuntimeError("dev_simulator has no OAuth; enable MOCK_PLATFORM_ENABLED and poll.")

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        raise RuntimeError("dev_simulator has no OAuth.")

    def fetch_comments(self, external_stream_id: str, cursor: Optional[str]) -> CommentPage:
        if not settings.MOCK_PLATFORM_ENABLED:
            return CommentPage(comments=[], next_cursor=None)
        n = random.randint(0, 2)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        out = []
        for _ in range(n):
            name, txt = random.choice(self._SAMPLES)
            out.append(UnifiedComment(
                platform="dev_simulator",
                platform_comment_id=f"sim-{uuid.uuid4()}",
                author_name=name, author_id=f"sim-user-{name.lower()}",
                author_avatar_url=None,
                text=f"{txt} [{uuid.uuid4().hex[:6]}]",
                published_at=now, viewer_status={}, raw={},
                simulated=True,
            ))
        return CommentPage(comments=out, next_cursor=None)

    def post_comment(self, external_stream_id: str, text: str,
                     reply_to_comment_id: Optional[str] = None) -> str:
        return f"sim-reply-{uuid.uuid4()}"
