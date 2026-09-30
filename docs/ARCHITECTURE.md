# StreamForge — System Architecture

**StreamForge** is a multi-platform live-streaming, comment-management and AI-engagement
platform: a Web dashboard (Next.js) + native Android app (Kotlin/Jetpack Compose) on top of a
production backend (Django + DRF + Channels + Celery + PostgreSQL + Redis).

> Per requirement #49, this document covers: A. System Architecture, B. Technology Stack,
> D. API Architecture, E. Platform Integration Architecture, F. Streaming Architecture,
> G. Android Architecture, H. Security Model, I. Folder Structure, J. Roadmap.
> C (Database ER diagram) lives in `docs/DATABASE.md`.

---

## A. System Architecture

```
                        ┌─────────────────────────────────────────────┐
                        │              CLIENTS                        │
                        │  Next.js Web Dashboard   Kotlin Android App │
                        └───────────┬─────────────────────┬───────────┘
                                    │ HTTPS (REST)        │ HTTPS (REST)
                                    │ WSS  (real-time)    │ WSS + RTMP(S) push
                        ┌───────────▼─────────────────────▼───────────┐
                        │            API GATEWAY / REVERSE PROXY      │
                        │        (nginx: TLS termination, rate        │
                        │         limiting headers, static files)     │
                        └───────────────────────┬─────────────────────┘
                    ┌───────────────────────────┼───────────────────────────┐
                    │                           │                           │
        ┌───────────▼───────────┐   ┌───────────▼───────────┐   ┌───────────▼───────────┐
        │  Django Application   │   │  Django Channels      │   │  Ingest Endpoint      │
        │  (DRF REST API)       │   │  (WebSocket server,   │   │  (RTMP/RTMPS receiver │
        │  - auth/accounts      │   │   ASGI consumer group │   │   e.g. MediaMTX /     │
        │  - platforms/OAuth    │   │   per stream room)    │   │   nginx-rtmp) → FFmpeg│
        │  - streams/comments   │   └───────────┬───────────┘   │   transcode/re-mux    │
        │  - AI assistant       │               │               └───────────┬───────────┘
        │  - moderation/roles   │        channel_layer (Redis pub/sub)      │ RTMP fan-out
        │  - analytics/history  │               │                           ▼
        └───────────┬───────────┘               │                 YouTube / Facebook /
                  DB (PostgreSQL) ◄─────────────┴── Celery workers ──► Twitch / X ...
                        ▲                        (pollers, webhooks,
                        │                         AI jobs, bots)
                   Redis (broker + channel layer + rate limits + cache)
```

### Process topology (docker-compose services)

| Service        | Image / build                | Responsibility |
|----------------|------------------------------|----------------|
| `web`          | Next.js frontend             | Dashboard UI, browser capture & streaming controls |
| `api`          | Django + Uvicorn (ASGI)      | REST API **and** WebSocket consumers (Channels) |
| `worker`       | Django + Celery              | Platform pollers, webhook processing, AI generation, bot scheduling |
| `beat`         | Django + Celery Beat         | Periodic tasks (comment polling cadence, token-refresh checks) |
| `db`           | PostgreSQL 15                | Primary datastore |
| `redis`        | Redis 7                      | Channel layer, Celery broker, rate-limit/cache store |
| `ingest`       | MediaMTX                     | Accepts RTMP/RTMPS/WebRTC from clients, serves HLS for monitoring, feeds restreamers |
| `restreamer`   | ffmpeg image                 | One process per active platform output (spawned via Celery orchestration) |
| `proxy`        | nginx                        | TLS, routing `/api`, `/ws`, `/media` |

### Real-time data flow for comments (requirement §18)

```
Platform API / Webhook
   → Integration service adapter (per-platform)
   → Celery queue (message queue – Redis broker; BullMQ-equivalent role)
   → Comment processor (dedupe, moderation filter, first-time-viewer detect)
   → PostgreSQL (persist)
   → Channels group_send("stream.{uuid}") over Redis channel layer
   → WebSocket → Creator Dashboard (Web) & Android app
```

Comments are **never fabricated**. In development without credentials the pipeline can run with an
explicitly-labelled `dev_simulator` integration (`MOCK_PLATFORM_ENABLED=false` by default; when
enabled every simulated comment is tagged `"simulated": true` and the UI shows a DEV badge).
See `docs/TROUBLESHOOTING.md` §"Why do I see no comments?" for the honest setup path.

---

## B. Technology Stack & Rationale

| Layer | Choice | Why |
|-------|--------|-----|
| Backend framework | **Django 5 + DRF** | Mature auth/ORM/admin; Option A of §19. Built-in admin seeds the Admin panel (§40). |
| Real-time | **Django Channels + Redis channel layer** | Same language/process model as the API; groups map 1:1 to stream rooms. |
| Async jobs | **Celery + Redis broker** | Long-running platform pollers, webhook retries, AI calls, FFmpeg orchestration. |
| Database | **PostgreSQL 15** | Relational integrity for the normalized schema in §17; JSONB for raw platform payloads. |
| Streaming ingest | **MediaMTX + FFmpeg** | Accepts RTMP/RTMPS/HLS/WebRTC; FFmpeg re-muxes (not re-encodes where codecs match) to each platform's required bitrate/resolution — no single-protocol assumption (§3). |
| Web frontend | **Next.js 14 + TypeScript + Tailwind** | SSR dashboard, `getUserMedia` capture, Web Speech API for voice typing (§13), Notification API (§24). |
| Mobile | **Kotlin + Jetpack Compose + CameraX + MediaCodec + RTMP publisher** | Native camera/audio capture, hardware H.264/AAC encoding, RTMPS push, foreground service (§21–22). Not a WebView wrapper. |
| AI | **Provider adapter layer** (§35) | `AIProvider` interface with OpenAI / Anthropic / Google / Local implementations, chosen by config. |
| Crypto | **AES-256-GCM envelope encryption** for OAuth tokens at rest (§17/§30) | Key from `TOKEN_ENCRYPTION_KEY` env var; keys never leave the backend. |

---

## D. API Architecture

Base: `https://<host>/api/v1` — versioned, JWT-authenticated (HttpOnly refresh cookie for web;
Bearer tokens for mobile). CSRF protection enabled for cookie-auth web flows; webhook endpoints
are signature-verified instead of session-authenticated.

Key endpoint groups (full list in `docs/API.md`):

```
/auth/*              register, login, logout, verify-email, password-reset, 2FA, sessions
/platforms/*         list, oauth begin/callback, disconnect, status
/streams/*           CRUD, start, stop, health, key (server-side ingest key)
/comments/*          list, reply, moderate, filters, search
/ai/*                settings, generate-response, rules, automation modes, usage
/moderators/*        invite (link/code/role), list, revoke
/devices/*           authorize (QR/pairing code), list, revoke
/notifications/*     list, mark-read
/analytics/*         stream stats, engagement, history search
/webhooks/<plat>/    verified platform callbacks (YouTube push, FB page subscriptions, Twitch EventSub)
/ws/streams/<uuid>/  WebSocket: comments, presence, health, AI suggestions, notifications
```

Every non-public endpoint enforces authentication **and** object-level permission checks
(stream ownership / role capability matrix) — see `backend/apps/core/permissions.py`.

---

## E. Platform Integration Architecture (§2, §46)

```
core/integrations/base.py        PlatformIntegration (ABC)
 ├── capabilities()              declares what this platform supports (chat_read, chat_post,
 │                               live_broadcast, moderation, viewer_stats, new_viewer_events)
 ├── oauth_urls()/exchange()     official OAuth 2.0 (+PKCE where offered)
 ├── fetch_comments(cursor)      incremental comment pull OR webhook-driven ingestion
 ├── post_comment(text, reply_to)
 ├── get_stream_key()/start_stream()/stop_stream()
 ├── validate_webhook(headers, body)
 └── normalize_comment(raw) -> UnifiedComment
```

Adapters: `youtube.py`, `facebook.py`, `twitch.py`, `x.py`, `tiktok.py`, `instagram.py`.
Where a feature is not permitted by the public API, the adapter raises
`CapabilityNotSupportedError` carrying a human message naming the exact credential/approval
required (e.g. *"TikTok Live ingestion requires approved partner-developer credentials; set
TIKTOK_CLIENT_ID/TIKTOK_CLIENT_SECRET"*), which the UI renders honestly instead of faking behavior.

Polling vs webhook is chosen per platform by capability: YouTube (PubSubHubbub webhook + fallback
polling of liveChatMessages), Twitch (EventSub webhook + channel chat via IRC-over-WSS service),
Facebook Page (webhook subscriptions), X (API v2 filtered stream where entitlement exists, else
polling mentions). **No scraping. No ToS bypass.**

---

## F. Streaming Architecture (§3, §5, §22, §23)

1. **Android**: CameraX preview → `MediaCodec` HW encoder (H.264/HEVC + AAC) → RTMPS client →
   ingest server (MediaMTX). Reconnection w/ exponential backoff; network monitor pauses
   gracefully; adaptive-bitrate hook; thermal-throttle listener.
2. **Web**: `getUserMedia` preview + health metrics via `getStats()`; browser pushes to ingest via
   WebRTC (WHEP/WHIP through MediaMTX); server-side FFmpeg produces HLS for monitoring playback.
3. **Restream fan-out**: On `POST /streams/{id}/start`, Celery spawns one FFmpeg output job per
   selected platform using that platform's stored/obtained stream key. YouTube keys come from the
   official Live Broadcasts API; Twitch keys are user-pasted (API provisioning isn't public —
   surfaced explicitly in UX, not hidden).
4. **Health loop**: FFmpeg stats + platform APIs → `StreamSessions` metrics → WS topic
   `health.update` → dashboard gauges (bitrate, FPS, dropped frames, upload speed, per-platform
   status dots: CONNECTED / CONNECTING / LIVE / WARNING / DISCONNECTED / ERROR). Failure of one
   platform's restream job does not affect the others (§5).

---

## G. Android Architecture (§21, §22)

Layered structure (source in `android/`):

```
app/src/main/java/com/streamforge/
 ui/        Compose screens: Login, Register, Dashboard, GoLive(camera), Comments,
            AIAssistant, Moderators, DeviceAuth(QR scan), Notifications, Settings
 domain/    usecases (StartStream, SendReply, ApproveAiResponse, PairDevice…)
 data/      Retrofit API client, OkHttp WebSocket manager, Room offline cache, DataStore session
 stream/    CameraX capture → MediaCodec encode → RTMP(S) publisher → health reporter
 svc/       Foreground StreamingService (camera|microphone types) + FCM FirebaseMessagingService
```

Voice typing uses the system speech recognizer (`RecognizerIntent`) (§13). QR pairing uses ML Kit
barcode scanning → `POST /devices/authorize`. Build instructions: `docs/ANDROID_BUILD.md`.

---

## H. Security Model (§16, §30, §32, §38)

* Passwords: Argon2 (fallback PBKDF2-SHA256) — never plaintext. Email verification + reset tokens
  are single-use and hashed at rest. Optional TOTP 2FA. Session table + "logout all devices".
* Tokens: short-lived JWT access (15 min) + rotating refresh in HttpOnly/SameSite=Strict cookie;
  device-scoped session IDs; login notifications; audit rows for security events.
* OAuth secrets: AES-256-GCM encrypted in `PlatformTokens.access_token_enc`; decrypted only inside
  integration services; **never serialized to any API response** (§30).
* RBAC matrix (`Role` × `Permission`) enforced at queryset level and WS join time:
  Owner / Administrator / Moderator / CommentAssistant / Viewer.
* Webhooks: HMAC/signature verification + timestamp-skew check; unauthenticated ⇒ 403 (§32).
* Rate limiting per IP/user/endpoint (DRF throttles + Redis counters); AI budget caps & dedupe (§36).
* Input validation everywhere (DRF serializers), parameterized ORM queries (no string SQL),
  upload type/size validation, CSP/XSS headers, HTTPS/HSTS enforced at proxy.
* Device authorization: single-use expiring pairing codes rendered as QR; revocable anytime;
  owner sees device name, last-active, user, permissions, session status (§15).

---

## I. Repository Layout

```
/workspace
├── backend/            Django project (apps: accounts, platforms, streams, comments, ai,
│                       moderation, devices, analytics, core incl. integrations & providers)
├── frontend/           Next.js + TypeScript + Tailwind dashboard
├── android/            Kotlin + Jetpack Compose app (Gradle project)
├── infra/              docker-compose.yml, nginx.conf, mediamtx.yml, Dockerfiles
├── docs/               ARCHITECTURE (this file), DATABASE.md (ER), API.md, SECURITY.md,
│                       PLATFORM_INTEGRATIONS.md, ANDROID_BUILD.md, DEPLOYMENT.md, TESTING.md,
│                       ROADMAP.md, TROUBLESHOOTING.md, ENVIRONMENT.md
└── README.md
```

---

## J. Development Roadmap (§47)

| Phase | Deliverable | Status in this repo |
|-------|-------------|---------------------|
| 1 | Architecture, DB, auth, API skeleton, security foundation | ✅ implemented |
| 2 | OAuth + platform adapters | ✅ adapter framework + YouTube/Twitch/X/Facebook flows; TikTok/IG gated behind partner credentials (honest errors) |
| 3 | Streaming infra (ingest, restream, health) | ✅ implemented (MediaMTX + FFmpeg orchestration) |
| 4 | Comment engine + WebSocket delivery | ✅ implemented |
| 5 | AI assistant, personality, rules, cost control | ✅ implemented (provider adapters + budget/rate guards) |
| 6 | Moderators, roles, device pairing | ✅ implemented |
| 7 | Android app | ✅ native Kotlin/Compose source provided (buildable with Android SDK) |
| 8 | Analytics & history | ✅ implemented |
| 9–11 | Security review, tests, deployment | ✅ pytest suites included; deploy guides + compose included |

See `docs/ROADMAP.md` for the granular task breakdown.
