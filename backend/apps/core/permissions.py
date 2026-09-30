"""RBAC permission classes shared across apps (§14, §30).

Role capability matrix (also documented in docs/SECURITY.md):

                      Owner  Admin  Moderator  CommentAssistant  Viewer
manage_stream_settings   ✓      ✓       ✗           ✗             ✗
read_comments            ✓      ✓       ✓           ✓              ✓
reply_to_comments        ✓      ✓       ✓           ✓              ✗
delete/hide_comments     ✓      ✓       ✓           ✗              ✗
block_users              ✓      ✓       ✓           ✗              ✗
use_ai_suggestions       ✓      ✓       ✓           ✓              ✗
send_ai_responses        ✓      ✓       ✓           ✓(approved)    ✗
manage_moderators        ✓      ✓       ✗           ✗              ✗
authorize_devices        ✓      ✓       ✗           ✗              ✗
view_analytics           ✓      ✓       ✓           ✗              ✗
stop_automation          ✓      ✓       ✓           ✗              ✗
"""
from rest_framework.permissions import BasePermission, SAFE_METHODS

# Capability names used by Role.capabilities JSON list.
CAP_READ_COMMENTS = "comments.read"
CAP_REPLY_COMMENTS = "comments.reply"
CAP_MODERATE_COMMENTS = "comments.moderate"
CAP_BLOCK_USERS = "users.block"
CAP_AI_SUGGEST = "ai.suggest"
CAP_AI_SEND = "ai.send"
CAP_MANAGE_STREAM = "stream.manage"
CAP_MANAGE_TEAM = "team.manage"
CAP_AUTHORIZE_DEVICES = "devices.authorize"
CAP_VIEW_ANALYTICS = "analytics.view"
CAP_STOP_AUTOMATION = "automation.stop"

ROLE_CAPABILITIES = {
    "owner": [
        CAP_READ_COMMENTS, CAP_REPLY_COMMENTS, CAP_MODERATE_COMMENTS, CAP_BLOCK_USERS,
        CAP_AI_SUGGEST, CAP_AI_SEND, CAP_MANAGE_STREAM, CAP_MANAGE_TEAM,
        CAP_AUTHORIZE_DEVICES, CAP_VIEW_ANALYTICS, CAP_STOP_AUTOMATION,
    ],
    "administrator": [
        CAP_READ_COMMENTS, CAP_REPLY_COMMENTS, CAP_MODERATE_COMMENTS, CAP_BLOCK_USERS,
        CAP_AI_SUGGEST, CAP_AI_SEND, CAP_MANAGE_STREAM, CAP_VIEW_ANALYTICS,
        CAP_STOP_AUTOMATION,
    ],
    "moderator": [
        CAP_READ_COMMENTS, CAP_REPLY_COMMENTS, CAP_MODERATE_COMMENTS, CAP_BLOCK_USERS,
        CAP_AI_SUGGEST, CAP_AI_SEND, CAP_VIEW_ANALYTICS, CAP_STOP_AUTOMATION,
    ],
    "comment_assistant": [CAP_READ_COMMENTS, CAP_AI_SUGGEST, CAP_AI_SEND],
    "viewer": [CAP_READ_COMMENTS],
}


def stream_capability(user, stream, capability: str) -> bool:
    """Does `user` hold `capability` on `stream`? Owner always does; team members via role."""
    if user is None or not user.is_authenticated:
        return False
    if stream.owner_id == user.id:
        return True
    membership = getattr(stream, "memberships", None)
    if membership is None:
        from apps.moderation.models import StreamMembership

        membership = StreamMembership.objects.filter(stream=stream, user=user)
    member = membership.first() if hasattr(membership, "first") else membership.get()
    if not member or member.revoked_at is not None:
        return False
    caps = ROLE_CAPABILITIES.get(member.role.slug, [])
    return capability in caps and capability in (member.extra_capabilities or [])


class IsStreamParticipant(BasePermission):
    """Object-level check: any active relationship with the stream (owner or member)."""

    message = "You do not have access to this stream."

    def has_object_permission(self, request, view, obj):
        stream = getattr(obj, "stream", obj)
        if stream.owner_id == request.user.id:
            return True
        from apps.moderation.models import StreamMembership

        return StreamMembership.objects.filter(
            stream=stream, user=request.user, revoked_at__isnull=True
        ).exists()


class CanReadComments(BasePermission):
    message = "Your role cannot read comments on this stream."

    def has_object_permission(self, request, view, obj):
        stream = getattr(obj, "stream", obj)
        return stream_capability(request.user, stream, CAP_READ_COMMENTS)


class CanReplyToComments(BasePermission):
    message = "Your role cannot reply to comments."

    def has_permission(self, request, view):
        return request.method != "GET"

    def has_object_permission(self, request, view, obj):
        stream = getattr(obj, "stream", obj)
        return stream_capability(request.user, stream, CAP_REPLY_COMMENTS)


class CanModerateContent(BasePermission):
    message = "Your role cannot moderate content."

    def has_object_permission(self, request, view, obj):
        stream = getattr(obj, "stream", obj)
        return stream_capability(request.user, stream, CAP_MODERATE_COMMENTS)


class CanManageStream(BasePermission):
    message = "Only the owner or an administrator can change stream settings."

    def has_object_permission(self, request, view, obj):
        stream = getattr(obj, "stream", obj)
        return stream_capability(request.user, stream, CAP_MANAGE_STREAM)


class IsAdminStaffAPI(BasePermission):
    """Platform administrators for the ops console (§40). Never exposes credentials."""

    message = "Administrator access required."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)
