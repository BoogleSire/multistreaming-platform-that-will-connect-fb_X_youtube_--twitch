# StreamForge — Improvement Proposal: **Clips Studio** (Live Highlight Capture)

**Status:** Proposed → Phase 1 implemented in this change set
**Priority:** High (biggest creator-growth lever per dollar of infrastructure)
**Author:** Platform team
**Date:** 2026-10-01

---

## 1. Executive summary

StreamForge already records the *broadcast* path: clients push RTMP(S) into MediaMTX and FFmpeg
restreamers fan out to YouTube/Facebook/Twitch/X/TikTok (`docs/ARCHITECTURE.md`, ingest service).
What creators cannot do today is turn a 3-hour live stream into shareable moments without leaving
the app and re-uploading manually.

**Clips Studio** closes that loop:

1. The streamer presses **Clip** (dashboard button, hotkey `C`, or Android "Clip" FAB) at any moment.
2. A background worker cuts the last N seconds from the **already-recorded program feed**
   (rolling DVR segments produced by the existing ingest pipeline — no second encoder, no extra
   bandwidth from the broadcaster).
3. The clip lands in a library with platform watermarks-free MP4, ready for one-tap re-publishing
   through the adapters we already have (YouTube Shorts / TikTok / X upload where APIs permit;
   honest download fallback elsewhere).
4. **AI synergy (flagship improvement):** the comment engine knows when chat is *excited*. A burst
   of comments/reactions in a short window is the single best free signal of a highlight moment.
   An automatic "hype detector" can silently capture candidate clips around spikes so nothing great
   is ever missed, and the AI assistant can draft the title/description using the stream context
   (§37) and the actual chat around the moment.

This feature was chosen because it (a) reuses infrastructure that already exists, (b) is genuinely
feasible with official APIs only, and (c) converts StreamForge from "control room" into "growth
tool", which is what differentiates it from Restream/StreamYard.

---

## 2. Why this over other candidates

| Candidate | Impact | Cost to build | Verdict |
|---|---|---|---|
| **Clips Studio + hype-triggered auto-clips** | Very high (retention & growth driver) | Medium — FFmpeg/MediaMTX already in stack | ✅ This proposal |
| Real-time translation of the unified comment feed | Medium | High (paid AI on every comment; conflicts with §36 cost control) | Later |
| Viewer-facing public watch page (HLS) | Medium | Low, but platforms already host viewers | Defer |
| Clip monetization / subscription gating | Commercial | Blocked until §41 billing exists | Roadmap |

---

## 3. Scope

### In scope (Phase 1 — implemented now)
- Data model: `StreamClip` (+ rolling-segment assumptions), statuses, retention fields.
- Service layer: DVR-window cutting via FFmpeg, spawn-safe async job API.
- REST endpoints under `/api/v1/streams/{id}/clips` (list/create/get/delete) wired to RBAC
  permissions (`core/permissions.py`) and audit logging (`§30`).
- WebSocket events (`clip.creating`, `clip.ready`, `clip.failed`) through the existing
  `core.models.publish()` channel-layer helper so dashboards update in real time (§18).
- Hype-spike hook points documented and stubbed where the comment processor will call them
  (comment engine itself is Phase 4 of the master roadmap).
- Unit tests with mocked ffmpeg (no fake user-facing data — §46 respected: everything real,
  failures surfaced honestly).

### Out of scope (explicitly deferred)
- One-tap *publishing* to each platform (needs per-platform upload scopes; adapter capability
  flags will be extended in Phase 2 of this feature).
- Storage quota enforcement tied to plans (§41 billing not yet implemented).
- Cloud transcode presets (vertical 9:16 crop is implemented; HDR/GIF later).

---

## 4. Architecture

```
Client (Web hotkey C / Android FAB / hype-detector)
        │  POST /api/v1/streams/{id}/clips {lookback_seconds}
        ▼
Django API ──► validates RBAC (Owner/Admin/Mod) ──► StreamClip row (status=queued)
        │                                            │
        │ publish("clip.creating")                   ▼
        ▼                                     Celery task `render_clip`
Channels WS ◄── publish("clip.ready") ◄── FFmpeg: concat DVR segments in
                                           [now-N, now] → faststart MP4
                                           (copy-codec when possible, else
                                            libx264 veryfast + AAC)
                                           writes MEDIA_ROOT/clips/{stream}/{clip_id}.mp4
                                           status=ready | failed(+human message §39)
```

Key decisions:

- **No extra load on the broadcaster.** Clips come from server-side recordings of the ingest feed,
  never from re-capturing the device camera.
- **Rolling DVR**: the ingest service keeps `-f segment -segment_time 10` files with a retention
  window (default 6 h, env `CLIP_DVR_RETENTION_HOURS`). Cutting = concatenating whole segments, so
  even a crash mid-stream leaves clips recoverable.
- **Codec copy first**: if the DVR segment codec matches output container, `-c copy` makes a
  60 s clip render in <1 s; otherwise fall back to re-encode (needed for 9:16 crops).
- **Failure honesty (§39/§46)**: ffmpeg errors are captured, stored in `last_error` as an
  actionable message ("Ingest recording unavailable — check the ingest service"), surfaced to the
  dashboard, and audited. We never fabricate a "ready" clip.
- **Security (§30)**: clip files served from authenticated storage path only; IDs are UUIDs;
  delete performs privacy-compliant removal (§38) plus audit row.

## 5. Data model addition

`apps.clips.models.StreamClip`
| field | notes |
|---|---|
| id (UUID pk) | |
| stream FK → streams.Stream | cascade delete |
| created_by FK → User | SET_NULL — moderators may clip (§14) |
| source | `manual` \| `hotkey` \| `hype_auto` \| `api` |
| lookback_seconds / duration_target | clamped by plan caps later (§41) |
| aspect | `original` \| `vertical_916` |
| title / description | AI-drafted suggestion stored here after review (§8 approval flow) |
| status | queued → rendering → ready \| failed \| expired |
| file_path, file_size, duration_seconds, width, height | filled on success only |
| last_error | human-readable (§39) |
| trigger_comment_count, trigger_window_start/end | populated when source=hype_auto — real metrics only (§26) |
| expires_at | retention sweep marks `expired` and deletes file (§38) |

Indexes: `(stream, -created_at)`, `status`.

## 6. API surface (added to §31 list)

```
GET    /api/v1/streams/{id}/clips/            list (filters: status, source)
POST   /api/v1/streams/{id}/clips/            {lookback_seconds, aspect, title?} → 201 queued
GET    /api/v1/streams/{id}/clips/{clip_id}/  detail incl. download URL (auth-gated)
DELETE /api/v1/streams/{id}/clips/{clip_id}/  remove file + row, audit "clip_deleted"
POST   /api/v1/streams/{id}/clips/{clip_id}/regenerate   re-run render after ingest recovery
```

WS events on the existing stream room group: `clip.creating`, `clip.progress`, `clip.ready`,
`clip.failed`.

## 7. Hype-triggered auto-clips (AI synergy)

Once the Phase-4 comment processor lands, it maintains a Redis sliding counter per stream
(`hype:{stream_id}` INCR + ZSET timestamps). A Celery beat task samples it every 5 s:

```
if comments_in_last_15s >= max(20, 3 × baseline_per_15s) and not cooling_down(stream):
    create StreamClip(source="hype_auto", lookback=cfg.auto_lookback, ...)
```

Guards mirror §12/§36 safety: per-stream cooldown, max auto-clips/hour, owner opt-in toggle, and
the global **STOP ALL AUTOMATION** button pauses auto-clipping too (reuses
`Stream.automation_stopped`). Auto-clips are *captured silently* but never published — creators
review them in the library, keeping humans in the loop.

## 8. UX placement

- Web dashboard: **Clip ✂** button next to Start/Stop + `C` hotkey; side panel "Recent clips" fed
  by WS events; Clips page with grid, filters, download, and (Phase 2) Share-to-platform.
- Android: persistent notification during live broadcast gains a "Clip last 60 s" action; in-app
  FAB on the Live screen. No WebView involved (§21).

## 9. Rollout plan

1. ✅ Phase 1 (this change): models, service, API, WS events, tests, migrations.
2. Phase 2: comment-engine hook + hype detector thresholds UI.
3. Phase 3: share/upload adapters (YouTube Shorts, TikTok Content Posting API — both gated behind
   their real approval processes; capability matrix in `base.py` extended with `CLIP_UPLOAD`).
4. Phase 4: quotas per plan, analytics row ("clips created / downloaded / shared").

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Disk growth from DVR + clips | retention sweep task, per-plan caps later, S3 backend option |
| ffmpeg process sprawl | Celery concurrency limit on `clips` queue; cgroups/timeouts; kill-on-cancel |
| Keyframe gaps make `-c copy` cuts imprecise ± GOP | acceptable for v1 (±2 s); re-encode mode selectable |
| Auto-clips feel invasive | opt-in default OFF, visible badge `hype_auto`, one-click STOP ALL |
