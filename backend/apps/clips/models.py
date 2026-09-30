"""Clips Studio models (docs/IMPROVEMENT_PROPOSAL.md).

A StreamClip is a short MP4 cut from the server-side rolling DVR recording of a stream's
ingest feed. Clips are captured, never auto-published — creators review/download/share them.
All render results are real: `status=ready` is only set after FFmpeg actually produced a file
with verified duration (§46 — no fabricated media).
"""
import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel

CLIP_SOURCES = [
    ("manual", "Manual button"),
    ("hotkey", "Keyboard hotkey"),
    ("android", "Android app"),
    ("hype_auto", "Hype auto-clip"),
    ("api", "API"),
]

CLIP_ASPECTS = [
    ("original", "Original aspect ratio"),
    ("vertical_916", "Vertical 9:16 (Shorts / Reels)"),
]

CLIP_STATUS = [
    ("queued", "Queued"),
    ("rendering", "Rendering"),
    ("ready", "Ready"),
    ("failed", "Failed"),
    ("expired", "Expired (retention)"),
]


class StreamClip(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stream = models.ForeignKey("streams.Stream", on_delete=models.CASCADE, related_name="clips")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="clips_created")

    source = models.CharField(max_length=12, choices=CLIP_SOURCES, default="manual")
    aspect = models.CharField(max_length=12, choices=CLIP_ASPECTS, default="original")

    # The clip window is anchored at request time and covers the previous `lookback_seconds`
    # of the DVR recording. Clamped so one clip can't hog the whole worker pool (§36 spirit).
    requested_at = models.DateTimeField(default=timezone.now)
    lookback_seconds = models.PositiveIntegerField(
        default=60, validators=[MinValueValidator(5), MaxValueValidator(600)])

    title = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)

    status = models.CharField(max_length=10, choices=CLIP_STATUS, default="queued", db_index=True)
    last_error = models.TextField(blank=True)          # actionable human message (§39)

    # Filled ONLY after a successful render — never guessed (§26/§46).
    file_path = models.CharField(max_length=500, blank=True)
    file_size_bytes = models.BigIntegerField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    rendered_at = models.DateTimeField(null=True, blank=True)

    # Provenance for hype-triggered clips: REAL counts from the comment engine only.
    trigger_comment_count = models.PositiveIntegerField(null=True, blank=True)
    trigger_window_start = models.DateTimeField(null=True, blank=True)
    trigger_window_end = models.DateTimeField(null=True, blank=True)

    expires_at = models.DateTimeField(null=True, blank=True)   # retention sweep (§38)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["stream", "-created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["expires_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(lookback_seconds__gte=5, lookback_seconds__lte=600),
                name="clip_lookback_bounds",
            ),
        ]

    def __str__(self):
        return f"clip {self.id} ({self.status}) of stream {self.stream_id}"

    @property
    def download_url(self) -> str:
        """Media-relative URL; access is enforced by auth middleware, not by obscurity (§30)."""
        if self.status != "ready" or not self.file_path:
            return ""
        return f"{settings.MEDIA_URL}{self.file_path}"

    @property
    def window_end(self):
        return self.requested_at

    @property
    def window_start(self):
        return self.requested_at - timezone.timedelta(seconds=self.lookback_seconds)
