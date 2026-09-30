"""Auth services: token issuance with server-side session tracking, TOTP 2FA (§16, §30).

Refresh tokens carry a `jti` claim bound to a Session row. Revoking a Session (or "log out
all devices") blacklists every refresh token for that user — enforced by the shared blacklist
app of simplejwt plus our own Session table (belt & braces, and gives us the human-readable
device list).
"""
from datetime import timedelta

import pyotp
from django.conf import settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.crypto import decrypt, encrypt

from .models import Session, User


def issue_tokens_for(user: User, request=None, device_label="", platform="unknown") -> tuple[str, str, Session]:
    """Create access+refresh JWTs and register the matching server-side Session."""
    refresh = RefreshToken.for_user(user)
    jti = str(refresh.get("jti") or refresh.access_token["jti"])
    session = Session.objects.create(
        user=user, jti=jti, device_label=device_label or _guess_device(request),
        platform=platform if platform != "unknown" else _guess_platform(request),
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("User-Agent", "")[:500] if request else ""),
        expires_at=timezone.now() + timedelta(days=settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].days),
    )
    return str(refresh.access_token), str(refresh), session


def _client_ip(request):
    if request is None:
        return None
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")) or None


def _guess_platform(request) -> str:
    ua = (request.headers.get("User-Agent", "") if request else "").lower()
    if "streamforge-android" in ua or "okhttp" in ua or "android" in ua:
        return "android"
    return "web"


def _guess_device(request) -> str:
    ua = request.headers.get("User-Agent", "") if request else ""
    return ua[:120]


def revoke_session(session: Session):
    session.revoked_at = timezone.now()
    session.save(update_fields=["revoked_at"])
    try:
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken, OutstandingToken,
        )

        for ot in OutstandingToken.objects.filter(jti=session.jti):
            BlacklistedToken.objects.get_or_create(token=ot)
    except Exception:  # blacklist app not migrated yet — Session check still blocks reuse
        pass


def logout_all_devices(user: User) -> int:
    count = 0
    for s in Session.objects.filter(user=user, revoked_at__isnull=True):
        revoke_session(s)
        count += 1
    return count


# ------------------------------------------------------------------ TOTP 2FA (§16)
def begin_totp_enrollment(user: User) -> str:
    secret = pyotp.random_base32()
    user.totp_secret_enc = encrypt(secret)
    user.save(update_fields=["totp_secret_enc"])
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="StreamForge")
    return uri


def confirm_totp_enrollment(user: User, code: str) -> bool:
    secret = decrypt(user.totp_secret_enc)
    if secret and pyotp.TOTP(secret).verify(code, valid_window=1):
        user.totp_enabled = True
        user.save(update_fields=["totp_enabled"])
        return True
    return False


def verify_totp(user: User, code: str) -> bool:
    if not user.totp_secret_enc:
        return False
    return pyotp.TOTP(decrypt(user.totp_secret_enc)).verify(code, valid_window=1)


def disable_totp(user: User):
    user.totp_enabled = False
    user.totp_secret_enc = ""
    user.save(update_fields=["totp_enabled", "totp_secret_enc"])


# ------------------------------------------------------------------ verification / reset (§16)
import hashlib


def create_single_use_token(user: User, purpose: str, ttl_minutes: int) -> str:
    from .models import PasswordChangeToken

    raw, digest = PasswordChangeToken.new_token()
    PasswordChangeToken.objects.update_or_create(
        user=user, purpose=purpose,
        defaults={"token_hash": digest, "expires_at": timezone.now() + timedelta(minutes=ttl_minutes),
                  "used_at": None},
    )
    return raw


def consume_token(raw: str, purpose: str) -> User | None:
    from .models import PasswordChangeToken

    digest = hashlib.sha256(raw.encode()).hexdigest()
    tok = PasswordChangeToken.objects.filter(token_hash=digest, purpose=purpose,
                                             used_at__isnull=True,
                                             expires_at__gt=timezone.now()).select_related("user").first()
    if not tok:
        return None
    tok.used_at = timezone.now()
    tok.save(update_fields=["used_at"])
    return tok.user
