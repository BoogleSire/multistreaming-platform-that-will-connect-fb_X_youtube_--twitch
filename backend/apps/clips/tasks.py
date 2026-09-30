"""Celery jobs for Clips Studio (§18). Queue `clips` is concurrency-limited so a burst of
clip requests can never starve comment ingestion or token-refresh workers."""
from celery import shared_task
from django.utils import timezone

from .models import StreamClip


@shared_task(bind=True, queue="clips", max_retries=2, default_retry_delay=15)
def render_clip(self, clip_id: str):
    """Render one queued clip. Transient failures retry with backoff; final failure is
    surfaced to the user as an actionable message (§39), never swallowed."""
    from .services import render_clip_sync

    try:
        clip = StreamClip.objects.get(pk=clip_id)
    except StreamClip.DoesNotExist:
        return {"clip_id": clip_id, "result": "gone"}
    if clip.status not in ("queued", "rendering"):
        return {"clip_id": clip_id, "result": clip.status}   # idempotent re-delivery guard
    try:
        clip = render_clip_sync(clip)
    except Exception as exc:                      # unexpected infra error → honest retry
        logger_msg = f"unexpected render error for clip {clip_id}: {exc}"
        import logging

        logging.getLogger("streamforge.clips").exception(logger_msg)
        raise self.retry(exc=exc)
    return {"clip_id": clip_id, "result": clip.status}


@shared_task(queue="clips")
def sweep_expired_clips():
    """Beat job (§38 retention): delete files + mark rows expired past CLIP_RETENTION_DAYS."""
    from pathlib import Path

    from django.conf import settings

    qs = (StreamClip.objects.filter(status__in=("ready", "expired"),
                                     expires_at__lt=timezone.now())
          .exclude(file_path=""))
    removed = 0
    for clip in qs.iterator():
        try:
            (Path(settings.MEDIA_ROOT) / clip.file_path).unlink(missing_ok=True)
        except OSError:
            continue      # will retry next sweep; do not mark expired yet
        clip.status = "expired"
        clip.file_path = ""
        clip.save(update_fields=["status", "file_path", "updated_at"])
        removed += 1
    return {"expired": removed}
