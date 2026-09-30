"""Unit tests for Clips Studio (§44).

FFmpeg/ffprobe are mocked at the subprocess boundary — tests verify our orchestration and
error honesty, not the FFmpeg project itself. No fake user-facing data is produced (§46):
`status=ready` requires a real file written by the (mocked) renderer in these tests.
"""
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.clips.models import StreamClip
from apps.clips.services import find_segments, render_clip_sync, request_clip
from apps.streams.models import Stream


def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout,
                                       stderr=stderr)


class ClipServiceTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.user = User.objects.create_user("caster", "c@example.com", "pw12345!")
        self.stream = Stream.objects.create(owner=self.user, title="Friday Q&A")
        self.dvr = self.tmp / "dvr"
        self.media = self.tmp / "media"
        self.work = self.tmp / "work"
        self.patcher = override_settings(
            CLIP_DVR_ROOT=str(self.dvr), MEDIA_ROOT=str(self.media),
            CLIP_WORK_ROOT=str(self.work), CLIP_SEGMENT_SECONDS=10,
        )
        self.patcher.enable()
        self.addCleanup(self.patcher.disable)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # ---- helpers -----------------------------------------------------
    def make_clip(self, lookback=30, aspect="original"):
        return StreamClip.objects.create(
            stream=self.stream, created_by=self.user, lookback_seconds=lookback,
            aspect=aspect, requested_at=timezone.now())

    def write_segments(self, count=5, size=100):
        d = self.dvr / str(self.stream.id)
        d.mkdir(parents=True, exist_ok=True)
        base = int(time.time()) - count * 10
        for i in range(count):
            (d / f"segment-{base + i * 10}.ts").write_bytes(b"\x00" * size)
        return d

    def ffmpeg_writes_file(self, cmd, **kw):
        """Simulate ffmpeg: the last arg is the output path."""
        Path(cmd[-1]).write_bytes(b"mp4data" * 100)
        return _fake_completed()

    @staticmethod
    def ffprobe_ok(duration=28.4, width=1280, height=720):
        def side_effect(cmd, **kw):
            if "format=duration" in " ".join(cmd):
                return _fake_completed(stdout=json.dumps({"format": {"duration": duration}}))
            return _fake_completed(stdout=json.dumps(
                {"streams": [{"width": width, "height": height}]}))
        return side_effect

    # ---- segment selection -------------------------------------------
    def test_find_segments_none_when_no_recording(self):
        self.assertIsNone(find_segments(self.stream.id,
                                       timezone.now() - timezone.timedelta(seconds=60),
                                       timezone.now()))

    def test_find_segments_skips_empty_inprogress_segment(self):
        d = self.write_segments(3)
        (d / f"segment-{int(time.time())}.ts").write_bytes(b"")   # still being written
        win = find_segments(self.stream.id,
                            timezone.now() - timezone.timedelta(seconds=60),
                            timezone.now())
        self.assertIsNotNone(win)
        self.assertEqual(len(win.paths), 3)

    # ---- render success ------------------------------------------------
    def test_render_success_sets_ready_with_real_file_and_metrics(self):
        self.write_segments()
        clip = self.make_clip()
        with mock.patch("apps.clips.services._run") as run, \
             mock.patch("apps.clips.services.publish") as pub:
            run.side_effect = lambda cmd, timeout=None: (
                self.ffmpeg_writes_file(cmd) if "-f" in cmd and "concat" in cmd
                else self.ffprobe_ok()(cmd))
            out = render_clip_sync(clip)
        self.assertEqual(out.status, "ready")
        self.assertTrue((self.media / out.file_path).is_file())
        self.assertEqual(out.duration_seconds, 28.4)
        self.assertEqual((out.width, out.height), (1280, 720))
        self.assertGreater(out.file_size_bytes, 0)
        self.assertIsNotNone(out.expires_at)
        events = [c.args[1] for c in pub.call_args_list]
        self.assertIn("clip.ready", events)

    # ---- honest failures (§39/§46) ------------------------------------
    def test_render_fails_honestly_without_recording(self):
        clip = self.make_clip()
        with mock.patch("apps.clips.services.publish"):
            out = render_clip_sync(clip)
        self.assertEqual(out.status, "failed")
        self.assertIn("Ingest recording unavailable", out.last_error)
        self.assertEqual(out.file_path, "")          # never fabricate a ready clip

    def test_render_ffmpeg_error_is_reported_not_swallowed(self):
        self.write_segments()
        clip = self.make_clip()
        with mock.patch("apps.clips.services._run",
                        return_value=_fake_completed(returncode=1, stderr="boom")), \
             mock.patch("apps.clips.services.publish"):
            out = render_clip_sync(clip)
        self.assertEqual(out.status, "failed")
        self.assertIn("media renderer", out.last_error)

    def test_render_rejects_invalid_probe_duration(self):
        self.write_segments()
        clip = self.make_clip()

        def run(cmd, timeout=None):
            if "-show_entries" in cmd:
                return _fake_completed(stdout=json.dumps({"format": {"duration": 0.2}}))
            Path(cmd[-1]).write_bytes(b"x" * 50)
            return _fake_completed()

        with mock.patch("apps.clips.services._run", side_effect=run), \
             mock.patch("apps.clips.services.publish"):
            out = render_clip_sync(clip)
        self.assertEqual(out.status, "failed")
        self.assertIn("validation", out.last_error)

    # ---- request validation --------------------------------------------
    def test_request_clip_rejects_out_of_bounds_lookback(self):
        with self.assertRaises(ValidationError):
            request_clip(stream=self.stream, created_by=self.user, lookback_seconds=3)

    def test_request_clip_clamps_and_queues(self):
        with mock.patch("apps.clips.tasks.render_clip.delay") as delay, \
             mock.patch("apps.clips.services.publish"), \
             mock.patch("apps.clips.services.audit", create=True):
            clip = request_clip(stream=self.stream, created_by=self.user,
                                lookback_seconds=60, source="hotkey")
        self.assertEqual(clip.status, "queued")
        delay.assert_called_once_with(str(clip.id))
