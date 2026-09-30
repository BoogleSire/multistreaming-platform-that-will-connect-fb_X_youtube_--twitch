"""Clips Studio service layer (docs/IMPROVEMENT_PROPOSAL.md §4).

Rendering pipeline, honestly described:

  1. The ingest service (MediaMTX + FFmpeg recorder) writes rolling DVR segments per stream:
        {CLIP_DVR_ROOT}/{stream_id}/segment-<epoch>.ts   (fixed 10 s GOP-aligned segments)
  2. `render_clip` selects whole segments overlapping [requested_at - lookback, requested_at],
     concatenates them with FFmpeg (codec copy when the aspect is `original`, re-encode for
     vertical 9:16 crops), and verifies the output duration with ffprobe.
  3. Only after step 2 succeeds does a clip become `ready`. Every failure path stores an
     actionable message in `last_error` and emits a WS `clip.failed` event (§39). We never
     mark a clip ready without a real file on disk (§46).
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.models import publish

from .models import StreamClip

logger = logging.getLogger("streamforge.clips")

SEGMENT_SUFFIX = ".ts"


@dataclass
class SegmentWindow:
    start: float          # unix epoch of first segment
    end: float            # unix epoch at end of last segment
    paths: list[Path]


def dvr_dir(stream_id) -> Path:
    return Path(settings.CLIP_DVR_ROOT) / str(stream_id)


def find_segments(stream_id, window_start, window_end) -> SegmentWindow | None:
    """Whole DVR segments overlapping [window_start, window_end].

    Returns None when the recording is missing/incomplete — callers turn that into an honest
    user-facing error instead of silently producing a short or empty clip.
    """
    directory = dvr_dir(stream_id)
    if not directory.is_dir():
        return None
    lo, hi = window_start.timestamp(), window_end.timestamp()
    seg_len = settings.CLIP_SEGMENT_SECONDS
    picked: list[Path] = []
    earliest = latest = None
    for p in sorted(directory.glob(f"*{SEGMENT_SUFFIX}")):
        try:
            epoch = float(p.stem.rsplit("-", 1)[-1])
        except ValueError:
            continue
        if p.stat().st_size == 0:
            continue                      # segment still being written / crashed recorder
        if epoch + seg_len >= lo and epoch <= hi:
            picked.append(p)
            earliest = epoch if earliest is None else min(earliest, epoch)
            latest = epoch + seg_len if latest is None else max(latest, epoch + seg_len)
    if not picked:
        return None
    return SegmentWindow(start=earliest, end=latest, paths=picked)


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe_duration(path: Path) -> float | None:
    try:
        res = _run([settings.FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
                    "-of", "json", str(path)], timeout=30)
        if res.returncode != 0:
            return None
        return float(json.loads(res.stdout)["format"]["duration"])
    except Exception:
        return None


def probe_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        res = _run([settings.FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height", "-of", "json", str(path)], timeout=30)
        if res.returncode != 0:
            return None, None
        st = json.loads(res.stdout)["streams"][0]
        return st.get("width"), st.get("height")
    except Exception:
        return None, None


def _concat_list_file(clip: StreamClip, segments: list[Path]) -> Path:
    out = Path(settings.CLIP_WORK_ROOT) / f"{clip.id}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(f"file '{s.resolve()}'\n" for s in segments))
    return out


def _output_path(clip: StreamClip) -> Path:
    rel = Path("clips") / str(clip.stream_id) / f"{clip.id}.mp4"
    abs_path = Path(settings.MEDIA_ROOT) / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    return abs_path, rel   # type: ignore[return-value]


def render_clip_sync(clip: StreamClip) -> StreamClip:
    """Synchronous render used by the Celery task (and directly testable).

    Mutates & saves the clip row; emits WS events at each transition (§18).
    """
    clip.status = "rendering"
    clip.last_error = ""
    clip.save(update_fields=["status", "last_error", "updated_at"])
    publish(clip.stream_id, "clip.progress", {"clip_id": str(clip.id), "status": "rendering"})

    def fail(message: str) -> StreamClip:
        clip.status = "failed"
        clip.last_error = message
        clip.save(update_fields=["status", "last_error", "updated_at"])
        publish(clip.stream_id, "clip.failed",
                {"clip_id": str(clip.id), "detail": message, "hint": _HINTS.get(message, "")})
        logger.warning("clip %s failed: %s", clip.id, message)
        return clip

    win = find_segments(clip.stream_id, clip.window_start, clip.window_end)
    if win is None:
        return fail("Ingest recording unavailable — the DVR folder for this stream has no "
                    "segments covering the requested window.")

    target_abs, rel_path = _output_path(clip)
    tmp_abs = target_abs.with_suffix(".part.mp4")
    list_file = _concat_list_file(clip, win.paths)

    base = [settings.FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file)]
    if clip.aspect == "original":
        # Codec copy: fast, lossless; cut points snap to segment boundaries (± one segment).
        cmd = base + ["-c", "copy", "-movflags", "+faststart", str(tmp_abs)]
    else:
        w, h = 720, 1280
        cmd = base + ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                      "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(tmp_abs)]

    try:
        res = _run(cmd, timeout=settings.CLIP_RENDER_TIMEOUT_SECONDS)
        if res.returncode != 0 or not tmp_abs.is_file() or tmp_abs.stat().st_size == 0:
            tail = (res.stderr or "").strip().splitlines()[-3:]
            logger.error("ffmpeg error for clip %s: %s", clip.id, " | ".join(tail))
            return fail("The media renderer could not process this window of the recording. "
                        "Try again; if it repeats, check the ingest service health.")
    except subprocess.TimeoutExpired:
        return fail("Clip rendering timed out. Shorten the clip length or retry during "
                    "lower server load.")
    finally:
        list_file.unlink(missing_ok=True)

    duration = probe_duration(tmp_abs)
    if duration is None or duration < 1.0:
        tmp_abs.unlink(missing_ok=True)
        return fail("Rendered file failed validation (unreadable or under 1 second). Retry "
                    "with a different window.")

    tmp_abs.replace(target_abs)
    width, height = probe_dimensions(target_abs)

    clip.status = "ready"
    clip.file_path = str(rel_path)
    clip.file_size_bytes = target_abs.stat().st_size
    clip.duration_seconds = round(duration, 2)
    clip.width, clip.height = width, height
    clip.rendered_at = timezone.now()
    clip.expires_at = timezone.now() + timezone.timedelta(days=settings.CLIP_RETENTION_DAYS)
    clip.save(update_fields=["status", "file_path", "file_size_bytes", "duration_seconds",
                             "width", "height", "rendered_at", "expires_at", "updated_at"])
    publish(clip.stream_id, "clip.ready", {
        "clip_id": str(clip.id),
        "duration_seconds": clip.duration_seconds,
        "size_bytes": clip.file_size_bytes,
        "url": clip.download_url,
        "aspect": clip.aspect,
    })
    logger.info("clip %s ready (%.1fs)", clip.id, clip.duration_seconds)
    return clip


_HINTS = {
    "Ingest recording unavailable — the DVR folder for this stream has no "
    "segments covering the requested window.":
        "Make sure the stream was live while recording was enabled, or widen the clip window.",
}


def request_clip(*, stream, created_by, lookback_seconds: int, aspect: str = "original",
                 title: str = "", source: str = "manual",
                 trigger_meta: dict | None = None) -> StreamClip:
    """Create the queued row and enqueue rendering. Shared by API + hype detector."""
    if aspect not in dict(StreamClip._meta.get_field("aspect").choices):
        raise ValidationError("Unknown clip aspect.")
    if not 5 <= int(lookback_seconds) <= 600:
        raise ValidationError("Clip length must be between 5 and 600 seconds.")

    clip = StreamClip.objects.create(
        stream=stream, created_by=created_by, source=source, aspect=aspect,
        lookback_seconds=int(lookback_seconds), title=title[:200],
        requested_at=timezone.now(),
        trigger_comment_count=(trigger_meta or {}).get("comment_count"),
        trigger_window_start=(trigger_meta or {}).get("window_start"),
        trigger_window_end=(trigger_meta or {}).get("window_end"),
    )
    from apps.core.services import audit

    audit("clip_requested", user=created_by, target=clip,
          metadata={"stream_id": str(stream.id), "lookback": clip.lookback_seconds,
                    "source": source, "aspect": aspect})
    publish(stream.id, "clip.creating",
            {"clip_id": str(clip.id), "lookback_seconds": clip.lookback_seconds,
             "aspect": aspect, "source": source})
    from .tasks import render_clip

    render_clip.delay(str(clip.id))
    return clip


def delete_clip(clip: StreamClip, *, user) -> None:
    """Privacy-compliant deletion (§38): remove file then row, audited."""
    clip_id, stream_id = str(clip.id), clip.stream_id
    if clip.file_path:
        path = Path(settings.MEDIA_ROOT) / clip.file_path
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("could not unlink clip file %s", path)
    clip.delete()
    from apps.core.services import audit

    audit("clip_deleted", user=user, metadata={"clip_id": clip_id, "stream_id": str(stream_id)})
    publish(stream_id, "clip.deleted", {"clip_id": clip_id})
