"""Request-scoped context for audit logging + request IDs (§30, §39)."""
import contextvars
import uuid

from django.utils.deprecation import MiddlewareMixin

current_request = contextvars.ContextVar("streamforge_request", default=None)
request_id_var = contextvars.ContextVar("streamforge_request_id", default=None)


class RequestIDMiddleware(MiddlewareMixin):
    def process_request(self, request):
        rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.request_id = rid
        request_id_var.set(rid)

    def process_response(self, request, response):
        response["X-Request-ID"] = getattr(request, "request_id", "")
        return response


class AuditContextMiddleware(MiddlewareMixin):
    """Makes the active request available to model-level audit helpers without globals dicts."""

    def process_request(self, request):
        token = current_request.set(request)
        request._audit_ctx_token = token

    def process_response(self, request, response):
        token = getattr(request, "_audit_ctx_token", None)
        if token is not None:
            current_request.reset(token)
        return response
