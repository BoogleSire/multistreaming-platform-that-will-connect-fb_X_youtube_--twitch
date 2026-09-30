"""Domain exceptions + DRF exception handler (§39).

Rule: users get actionable messages, not raw codes.
  "YouTube authorization expired. Please reconnect your YouTube account."
not "Error 401".
"""
import logging
import uuid

from django.core.exceptions import PermissionDenied, ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger("streamforge.errors")


class PlatformError(APIException):
    """Base for integration failures; carries a user-facing message + recovery hint."""

    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The connected platform is currently unreachable. We will retry automatically."
    default_code = "platform_error"

    def __init__(self, detail=None, *, platform="", hint="", retryable=True, error_id=None):
        payload = {
            "detail": detail or self.default_detail,
            "platform": platform,
            "hint": hint,           # e.g. "Reconnect your YouTube account in Settings → Platforms"
            "retryable": retryable,
            "error_id": error_id or str(uuid.uuid4()),  # traceable in logs without leaking internals
        }
        super().__init__(payload)


class TokenExpiredError(PlatformError):
    status_code = status.HTTP_401_UNAUTHORIZED

    def __init__(self, platform, detail=None):
        super().__init__(
            detail
            or f"{platform.title()} authorization expired. Please reconnect your {platform.title()} account.",
            platform=platform,
            hint=f"Settings → Connected Platforms → Reconnect {platform.title()}",
            retryable=False,
        )


class CapabilityNotSupportedError(PlatformError):
    """Raised by adapters when the official API does not permit a feature (§2, §46)."""

    status_code = status.HTTP_501_NOT_IMPLEMENTED

    def __init__(self, platform, feature, requirement):
        super().__init__(
            f"{platform.title()} does not support {feature} through its public API.",
            platform=platform,
            hint=requirement,
            retryable=False,
        )


class AIProviderUnavailableError(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = (
        "The AI assistant is not configured on this deployment. Set AI_PROVIDER and the "
        "matching API key environment variable to enable suggestions."
    )
    default_code = "ai_unavailable"


class AIBudgetExceededError(APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = "AI response budget reached for this period. Suggestions pause until the next window."
    default_code = "ai_budget_exceeded"


def streamforge_exception_handler(exc, context):
    """Map Django core exceptions to DRF shape and log unexpected ones with an error id."""
    if isinstance(exc, Http404):
        exc = APIException({"detail": "The requested item was not found.", "code": "not_found"})
    elif isinstance(exc, PermissionDenied):
        exc = APIException({"detail": "You do not have permission to perform this action.",
                            "code": "permission_denied"}, status_code=403)
    elif isinstance(exc, DjangoValidationError):
        exc = APIException({"detail": exc.messages, "code": "invalid"}, status_code=400)

    response = drf_exception_handler(exc, context)
    if response is None:
        error_id = str(uuid.uuid4())
        logger.exception("Unhandled server error [error_id=%s]", error_id)
        return Response(
            {
                "detail": "Something went wrong on our side. The engineering team has been "
                          f"notified. Reference: {error_id}",
                "error_id": error_id,
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return response
