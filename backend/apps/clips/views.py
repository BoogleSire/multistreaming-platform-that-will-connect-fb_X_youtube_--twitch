"""Clips Studio API (§31 style, docs/IMPROVEMENT_PROPOSAL.md §6)."""
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import CAP_MANAGE_STREAM, stream_capability
from apps.core.services import audit
from apps.streams.models import Stream

from .models import StreamClip
from .services import delete_clip, render_clip_sync, request_clip


class ClipSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.display_name", read_only=True)
    download_url = serializers.CharField(read_only=True)
    hint = serializers.SerializerMethodField()

    class Meta:
        model = StreamClip
        fields = [
            "id", "stream", "source", "aspect", "status", "title", "description",
            "lookback_seconds", "requested_at", "created_at", "rendered_at",
            "duration_seconds", "width", "height", "file_size_bytes",
            "download_url", "last_error", "hint", "created_by_name",
            "trigger_comment_count", "expires_at",
        ]
        read_only_fields = fields

    def get_hint(self, obj):
        """Actionable recovery text instead of raw errors (§39)."""
        if obj.status == "failed" and "Ingest recording unavailable" in obj.last_error:
            return "Settings → Streaming: confirm server-side recording is enabled for this stream."
        return ""


class ClipCreateSerializer(serializers.Serializer):
    lookback_seconds = serializers.IntegerField(min_value=5, max_value=600, default=60)
    aspect = serializers.ChoiceField(choices=list(dict(StreamClip._meta
                                                       .get_field("aspect").choices)),
                                     default="original")
    title = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    source = serializers.ChoiceField(choices=[("manual", "manual"), ("hotkey", "hotkey"),
                                              ("android", "android"), ("api", "api")],
                                     default="manual")


def _can_clip(user, stream) -> bool:
    """Owner/admin/moderator may capture clips; comment assistants & viewers may not (§14)."""
    return stream_capability(user, stream, CAP_MANAGE_STREAM)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def clips_collection(request, stream_id):
    stream = Stream.objects.filter(pk=stream_id).first()
    if stream is None:
        return Response({"detail": "Stream not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _can_clip(request.user, stream):
        raise PermissionDenied("Your role cannot manage clips on this stream.")

    if request.method == "GET":
        qs = stream.clips.select_related("created_by")
        for field in ("status", "source"):
            value = request.query_params.get(field)
            if value:
                qs = qs.filter(**{field: value})
        page = qs[:200]
        return Response(ClipSerializer(page, many=True).data)

    ser = ClipCreateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    try:
        clip = request_clip(stream=stream, created_by=request.user,
                            **ser.validated_data)
    except DjangoValidationError as exc:
        raise ValidationError(str(exc))
    return Response(ClipSerializer(clip).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "DELETE", "POST"])
@permission_classes([IsAuthenticated])
def clip_detail(request, stream_id, clip_id):
    clip = StreamClip.objects.filter(pk=clip_id, stream_id=stream_id).select_related("stream") \
        .first()
    if clip is None:
        return Response({"detail": "Clip not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _can_clip(request.user, clip.stream):
        raise PermissionDenied("Your role cannot manage clips on this stream.")

    if request.method == "GET":
        return Response(ClipSerializer(clip).data)

    if request.method == "DELETE":
        delete_clip(clip, user=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # POST .../regenerate — allowed only for failed clips after ingest recovery (§39).
    if clip.status != "failed":
        return Response({"detail": "Only failed clips can be regenerated."},
                        status=status.HTTP_409_CONFLICT)
    audit("clip_regenerated", user=request.user, target=clip)
    render_clip_sync(clip)
    return Response(ClipSerializer(clip).data)
