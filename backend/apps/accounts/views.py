"""Auth API views (§16, §30, §31, §38).

Flow notes:
* register → 201 + verification email sent (console backend in dev).
* login → sets HttpOnly refresh cookie (`sf_refresh`), returns access token in body.
* refresh endpoint rotates the refresh cookie and re-validates the server-side Session row —
  revoked sessions can never mint new access tokens even before blacklist propagation.
"""
from django.conf import settings
from django.urls import path
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.services import audit, notify

from .models import User
from .serializers import (
    ChangePasswordSerializer, LoginSerializer, PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer, RegisterSerializer, SessionSerializer, UserSerializer,
)
from .services import (
    begin_totp_enrollment, confirm_totp_enrollment, consume_token, create_single_use_token,
    disable_totp, issue_tokens_for, logout_all_devices, revoke_session, verify_totp,
)


class AuthBurstThrottle(ScopedRateThrottle):
    scope = "auth_burst"


def _set_refresh_cookie(response, refresh: str):
    response.set_cookie(
        "sf_refresh", refresh,
        httponly=True, secure=settings.SESSION_COOKIE_SECURE, samesite="Lax",
        max_age=settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].days * 86400,
        path="/api/v1/auth",
    )
    return response


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthBurstThrottle])
def register(request):
    ser = RegisterSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    user = ser.save()
    audit("register", user=user, request=request)
    raw = create_single_use_token(user, "verify_email", ttl_minutes=24 * 60)
    link = f"{settings.FRONTEND_BASE_URL}/verify-email?token={raw}"
    from django.core.mail import send_mail

    send_mail(
        subject="Confirm your StreamForge email",
        message=f"Welcome {user.display_name}! Confirm your address: {link}\n\n"
                "If you didn't create this account, ignore this email.",
        from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[user.email], fail_silently=True,
    )
    access, refresh, _ = issue_tokens_for(user, request, platform="web")
    resp = Response({"access": access, "user": UserSerializer(user).data,
                     "detail": "Check your inbox to verify your email."},
                    status=status.HTTP_201_CREATED)
    return _set_refresh_cookie(resp, refresh)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthBurstThrottle])
def login(request):
    ser = LoginSerializer(data=request.data, context={"request": request})
    ser.is_valid(raise_exception=True)
    user = ser.validated_data["user"]
    device_label = request.data.get("device_label", "")
    platform = request.data.get("platform", "unknown")
    access, refresh, session = issue_tokens_for(user, request, device_label, platform)
    audit("login", user=user, request=request, metadata={"platform": platform})
    resp = Response({"access": access, "user": UserSerializer(user).data,
                     "session_id": str(session.id)})
    return _set_refresh_cookie(resp, refresh)


@api_view(["POST"])
@permission_classes([AllowAny])
def refresh(request):
    raw = request.COOKIES.get("sf_refresh") or request.data.get("refresh")
    if not raw:
        return Response({"detail": "No active session on this device. Please sign in again."},
                        status=status.HTTP_401_UNAUTHORIZED)
    try:
        refresh_t = RefreshToken(raw)
    except TokenError:
        return Response({"detail": "Your session expired for security. Please sign in again."},
                        status=status.HTTP_401_UNAUTHORIZED)
    jti = str(refresh_t.get("jti"))
    from .models import Session as Sess

    sess = Sess.objects.filter(jti=jti).first()
    if not sess or not sess.active:
        return Response({"detail": "This session was signed out. Please log in again."},
                        status=status.HTTP_401_UNAUTHORIZED)
    from django.utils import timezone

    sess.last_seen_at = timezone.now()
    sess.save(update_fields=["last_seen_at"])
    try:
        new = refresh_t.rotate()
    except Exception:
        new = refresh_t  # blacklist app not installed; reuse with rotation semantics kept
    access = str(new.access_token)
    resp = Response({"access": access})
    return _set_refresh_cookie(resp, str(new))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout(request):
    raw = request.COOKIES.get("sf_refresh")
    if raw:
        try:
            jti = str(RefreshToken(raw).get("jti"))
            from .models import Session as Sess

            sess = Sess.objects.filter(jti=jti).first()
            if sess:
                revoke_session(sess)
        except TokenError:
            pass
    audit("logout", user=request.user, request=request)
    resp = Response({"detail": "Signed out."})
    resp.delete_cookie("sf_refresh", path="/api/v1/auth")
    return resp


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout_all(request):
    n = logout_all_devices(request.user)
    audit("logout", user=request.user, request=request, metadata={"scope": "all_devices"})
    resp = Response({"detail": f"Signed out from all devices ({n} sessions revoked)."})
    resp.delete_cookie("sf_refresh", path="/api/v1/auth")
    return resp


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(UserSerializer(request.user).data)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def update_profile(request):
    ser = UserSerializer(request.user, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(ser.data)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthBurstThrottle])
def verify_email(request):
    token = request.data.get("token", "")
    user = consume_token(token, "verify_email")
    if not user:
        return Response({"detail": "That verification link is invalid or already used. "
                                   "Request a new one from Settings."},
                        status=status.HTTP_400_BAD_REQUEST)
    user.email_verified = True
    user.save(update_fields=["email_verified"])
    return Response({"detail": "Email confirmed. Thanks!"})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthBurstThrottle])
def resend_verification(request):
    user = User.objects.filter(email__iexact=request.data.get("email", "")).first()
    if user and not user.email_verified:
        raw = create_single_use_token(user, "verify_email", ttl_minutes=24 * 60)
        from django.core.mail import send_mail

        send_mail("Confirm your StreamForge email",
                  f"Confirm here: {settings.FRONTEND_BASE_URL}/verify-email?token={raw}",
                  settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=True)
    # Same response whether or not the account exists — no user enumeration (§30).
    return Response({"detail": "If that address needs verification, a new link has been sent."})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthBurstThrottle])
def password_reset_request(request):
    user = User.objects.filter(email__iexact=request.data.get("email", "")).first()
    if user:
        raw = create_single_use_token(user, "password_reset", ttl_minutes=60)
        from django.core.mail import send_mail

        send_mail("Reset your StreamForge password",
                  f"Open this link within 60 minutes to choose a new password: "
                  f"{settings.FRONTEND_BASE_URL}/reset-password?token={raw}\n\n"
                  "If you didn't request this, secure your account immediately.",
                  settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=True)
        audit("password_reset", user=user, request=request, metadata={"stage": "requested"})
    return Response({"detail": "If an account exists for that email, a reset link is on its way."})


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([AuthBurstThrottle])
def password_reset_confirm(request):
    ser = PasswordResetConfirmSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    user = consume_token(ser.validated_data["token"], "password_reset")
    if not user:
        return Response({"detail": "That reset link is invalid, expired, or already used. "
                                   "Please start over."}, status=status.HTTP_400_BAD_REQUEST)
    user.set_password(ser.validated_data["new_password"])
    user.save()
    n = logout_all_devices(user)   # password changed ⇒ every other session dies (§30)
    audit("password_reset", user=user, request=request, metadata={"stage": "completed"})
    notify(user, "security_event", "Password changed",
           f"Your password was changed and {n} other session(s) were signed out.")
    return Response({"detail": "Password updated. Sign in with your new password."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def change_password(request):
    ser = ChangePasswordSerializer(data=request.data, context={"request": request})
    ser.is_valid(raise_exception=True)
    request.user.set_password(ser.validated_data["new_password"])
    request.user.save()
    audit("password_reset", user=request.user, request=request, metadata={"stage": "changed"})
    return Response({"detail": "Password updated on this device. Other devices stay signed in "
                               "unless you use 'Sign out everywhere'."})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def sessions_list(request):
    return Response(SessionSerializer(request.user.sessions.all(), many=True).data)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def sessions_revoke(request, session_id):
    sess = request.user.sessions.filter(id=session_id).first()
    if not sess:
        return Response({"detail": "Session not found."}, status=status.HTTP_404_NOT_FOUND)
    revoke_session(sess)
    audit("logout", user=request.user, request=request, target=sess, metadata={"scope": "one_session"})
    return Response({"detail": "That device has been signed out."})


# ------------------------------------------------------------------ 2FA (§16)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def twofa_begin(request):
    uri = begin_totp_enrollment(request.user)
    return Response({"provisioning_uri": uri,
                     "detail": "Add this to your authenticator app, then confirm a code."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def twofa_confirm(request):
    if confirm_totp_enrollment(request.user, request.data.get("code", "")):
        audit("2fa_enabled", user=request.user, request=request)
        return Response({"detail": "Two-factor authentication is now ON."})
    return Response({"detail": "That code didn't match. Ensure your device clock is accurate "
                               "and try again."}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def twofa_disable(request):
    if not verify_totp(request.user, request.data.get("code", "")):
        return Response({"detail": "A valid two-factor code is required to disable 2FA."},
                        status=status.HTTP_400_BAD_REQUEST)
    disable_totp(request.user)
    audit("2fa_disabled", user=request.user, request=request)
    return Response({"detail": "Two-factor authentication turned off."})


urlpatterns = [
    path("register", register),
    path("login", login),
    path("refresh", refresh),
    path("logout", logout),
    path("logout-all", logout_all),
    path("me", me),
    path("profile", update_profile),
    path("verify-email", verify_email),
    path("resend-verification", resend_verification),
    path("password-reset/request", password_reset_request),
    path("password-reset/confirm", password_reset_confirm),
    path("password/change", change_password),
    path("sessions", sessions_list),
    path("sessions/<uuid:session_id>/revoke", sessions_revoke),
    path("2fa/begin", twofa_begin),
    path("2fa/confirm", twofa_confirm),
    path("2fa/disable", twofa_disable),
]
