"""Stream domain models (§17): Streams / StreamPlatforms / StreamSessions."""
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel

VISIBILITY = [("public", "Public"), ("unlisted", "Unlisted"), ("private", "Private")]
CATEGORIES = [("just-chatting", "Just Chatting"), ("gaming", "Gaming"), ("music", "Music"),
              ("sports", "Sports"), ("news", "News"), ("education", "Education"),
              ("business", "Business & Finance"), ("food", "Food & Cooking"),
              ("travel", "Travel"), ("tech", "Tech")]
PLATFORM_STATUS = [
    ("pending", "Pending"), ("connecting", "Connecting"), ("live", "Live"),
    ("warning", "Warning"), ("disconnected", "Disconnected"), ("error", "Error"),
]


class Stream(TimeStampedModel):
    """One broadcast definition owned by a creator; may fan out to many platforms (§5)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="streams")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=40, choices=CATEGORIES, blank=True)
    thumbnail = models.ImageField(upload_to="stream_thumbs/", null=True, blank=True)
    visibility = models.CharField(max_length=10, choices=VISIBILITY, default="public")

    # ingest credentials for device push (§3): clients stream here, we restream onward
    ingest_stream_id = models.CharField(max_length=64, unique=True, db_index=True,
                                        default=lambda: f"sf{secrets.token_hex(8)}")

    ai_mode = models.CharField(max_length=12, default="approval",
                               choices=[("off", "Off"), ("approval", "Approval mode"),
                                        ("automatic", "Automatic mode")])  # §12
    bot_enabled = models.BooleanField(default=True)                        # welcome bot §11
    automation_paused_at = models.DateTimeField(null=True, blank=True)     # STOP ALL (§25)

    status = models.CharField(max_length=12, default="draft",
                              choices=[("draft", "Draft"), ("scheduled", "Scheduled"),
                                       ("starting", "Starting"), ("live", "Live"),
                                       ("ended", "Ended"), ("failed", "Failed")])
    scheduled_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "-created_at"]),
                   models.Index(fields=["status"])]

    def __str__(self):
        return f"{self.title} ({self.status})"

    @property
    def is_live(self):
        return self.status == "live"

    @property
    def automation_stopped(self) -> bool:
        """True if the emergency stop was pressed and not re-enabled this session."""
        return self.automation_paused_at is not None


class StreamPlatform(TimeStampedModel):
    """Join table: which connected platform account carries this stream (§5, §17)."""

    stream = models.ForeignKey(Stream, on_delete=models.CASCADE, related_name="platform_targets")
    connection = models.ForeignKey("platforms.ConnectedPlatform", on_delete=models.CASCADE,
                                   related_name="stream_platforms")
    external_id = models.CharField(max_length=128, blank=True)   # video/broadcast id on platform
    status = models.CharField(max_length=14, choices=PLATFORM_STATUS, default="pending")
    status_detail = models.TextField(blank=True)                 # human message (§39)
    rtmp_url = models.CharField(max_length=500, blank=True)      # per-platform ingest (encrypted? no — keys are)
    stream_key_enc = models.TextField(blank=True)                # encrypted platform key (§30)
    viewer_stats = models.JSONField(default=dict, blank=True)    # only real API numbers (§26)
    restream_job_id = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["stream", "connection"],
                                               name="uniq_stream_platform_connection")]
        verbose_name_plural = "Stream platforms"

    @property
    def platform(self):
        return self.connection.platform


class StreamSession(models.Model):
    """A single live run of a Stream with health metrics (§23, §26)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stream = models.ForeignKey(Stream, on_delete=models.CASCADE, related_name="sessions")
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    peak_concurrent_viewers = models.PositiveIntegerField(default=0)
    total_comments = models.PositiveIntegerField(default=0)
    total_replies = models.PositiveIntegerField(default=0)
    ai_responses_sent = models.PositiveIntegerField(default=0)
    bot_responses_sent = models.PositiveIntegerField(default=0)
    moderation_events = models.PositiveIntegerField(default=0)
    # latest client-reported health snapshot (bitrate/fps/dropped/upload kbps)
    health = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]

    @property
    def duration_seconds(self) -> int:
        end = self.ended_at or timezone.now()
        return max(0, int((end - self.started_at).total_seconds()))
