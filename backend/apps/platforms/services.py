"""Platform connection service: OAuth state, callback handling, token storage (§2, §30).

CSRF-safe design: `state` is a signed, expiring payload bound to the initiating user session.
Tokens are written through PlatformToken (encrypted fields) and never returned to clients.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.utils import timezone

from apps.core.crypto import encrypt
from apps.core.exceptions import PlatformError
from apps.core.integrations.base import get_integration_class, load_all_adapters

from .models import ConnectedPlatform, PlatformToken

_state_signer = TimestampSigner(key=f"{settings.SECRET_KEY}:oauth-state", salt="sf.oauth.state")


def begin_oauth(user, platform_key: str) -> tuple[str, str]:
    """Return (authorization_url, state). The frontend redirects the browser to the URL."""
    load_all_adapters()
    cls = get_integration_class(platform_key)
    if not cls.is_configured():
        raise PlatformError(
            f"{cls.display_name} isn't configured on this deployment yet. Set "
            f"{cls.settings_prefix}_CLIENT_ID / {cls.settings_prefix}_CLIENT_SECRET environment "
            "variables (see docs/PLATFORM_INTEGRATIONS.md), then try again.",
            platform=platform_key, retryable=False)
    state = _state_signer.sign(f"{user.id}:{platform_key}:{secrets.token_hex(8)}")
    redirect_uri = f"{settings.OAUTH_REDIRECT_BASE}/api/v1/platforms/callback/{platform_key}"
    adapter = cls({})
    return adapter.authorization_url(state=state, redirect_uri=redirect_uri), state


def complete_oauth(user_id: str, platform_key: str, state: str, code: str) -> ConnectedPlatform:
    """Validate state, exchange the official code, persist encrypted tokens."""
    try:
        raw = _state_signer.unsign(state, max_age=600)  # 10 minutes to finish consent
    except SignatureExpired:
        raise PlatformError("The authorization step took too long. Please start it again.",
                            platform=platform_key, retryable=False)
    except BadSignature:
        raise PlatformError("That authorization link is invalid or was tampered with. "
                            "Please start again from the dashboard.",
                            platform=platform_key, retryable=False)
    uid, plat, _nonce = raw.split(":")
    if str(uid) != str(user_id) or plat != platform_key:
        raise PlatformError("This authorization doesn't belong to your account.",
                            platform=platform_key, retryable=False)

    load_all_adapters()
    cls = get_integration_class(platform_key)
    redirect_uri = f"{settings.OAUTH_REDIRECT_BASE}/api/v1/platforms/callback/{platform_key}"
    adapter = cls(_client_credentials(platform_key))
    result = adapter.exchange_code(code, redirect_uri)

    account = result["account"]
    conn, _ = ConnectedPlatform.objects.update_or_create(
        user_id=user_id, platform=platform_key,
        external_account_id=str(account.get("id") or ""),
        defaults={
            "account_name": account.get("name") or "",
            "account_avatar_url": account.get("avatar_url") or None,
            "scopes": result.get("scope", ""),
            "status": "connected",
            "last_error": "",
            "extra": result.get("extra", {}),
            "disconnected_at": None,
            "connected_at": timezone.now(),
        },
    )
    PlatformToken.objects.update_or_create(
        connected=conn,
        defaults={
            "access_token_enc": result["access_token"],
            "refresh_token_enc": result.get("refresh_token") or "",
            "expires_at": result.get("expires_at"),
            "secret_blob_enc": json.dumps({k: v for k, v in result.items()
                                           if k.startswith("_")} | account_extra_secrets(result)),
        },
    )
    return conn


def account_extra_secrets(result: dict) -> dict:
    out = {}
    if result.get("user_access_token"):
        out["user_access_token"] = result["user_access_token"]
    if result.get("_pkce_verifier"):
        out["pkce_verifier"] = result["_pkce_verifier"]
    return out


def _client_credentials(platform_key: str) -> dict:
    from django.conf import settings as s

    prefix = get_integration_class(platform_key).settings_prefix
    return {"client_id": getattr(s, f"{prefix}_CLIENT_ID", ""),
            "client_secret": getattr(s, f"{prefix}_CLIENT_SECRET", "")}


def build_adapter(conn: ConnectedPlatform):
    """Instantiate the adapter with decrypted credentials — backend-side only (§30)."""
    load_all_adapters()
    cls = get_integration_class(conn.platform)
    creds = _client_credentials(conn.platform)
    tok = conn.token
    blob = {}
    if tok and tok.secret_blob_enc:
        try:
            blob = json.loads(tok.secret_blob_enc)
        except ValueError:
            blob = {}
    creds.update({
        "access_token": tok.access_token_enc if tok else "",
        "refresh_token": tok.refresh_token_enc if tok else "",
        "account_external_id": conn.external_account_id,
        "user_id": str(conn.user_id),
    })
    creds.update(blob)
    extra = conn.extra or {}
    if extra.get("has_live_scope"):
        creds["has_live_scope"] = True
    if extra.get("manual_stream_key"):
        creds["manual_stream_key"] = extra["manual_stream_key"]
    if extra.get("ingest_server"):
        creds["ingest_server"] = extra["ingest_server"]
    return cls(creds)


def disconnect(conn: ConnectedPlatform):
    conn.status = "disconnected"
    conn.disconnected_at = timezone.now()
    conn.save(update_fields=["status", "disconnected_at", "updated_at"])
    PlatformToken.objects.filter(connected=conn).delete()   # revoke our stored copy (§38)
