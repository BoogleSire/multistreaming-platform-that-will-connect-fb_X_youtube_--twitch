"""Authentication backends & DRF auth classes (§16, §30).

CookieJWTAuthentication: web SPA reads access token from JS memory but refresh lives in an
HttpOnly cookie; this class accepts `Authorization: Bearer` first, then falls back to the
cookie for refresh rotation flows.
"""
from rest_framework_simplejwt.authentication import JWTAuthentication


class CookieJWTAuthentication(JWTAuthentication):
    """Bearer header preferred; no credential leakage through referrer/logs."""

    def authenticate(self, request):
        header = self.get_header(request)
        if header is None:
            raw = request.COOKIES.get("sf_access")
            if not raw:
                return None
        else:
            raw = self.get_raw_authorization(header)
            if raw is None:
                return None
        access_token = self.get_validated_token(raw)
        return self.get_user(access_token), access_token
