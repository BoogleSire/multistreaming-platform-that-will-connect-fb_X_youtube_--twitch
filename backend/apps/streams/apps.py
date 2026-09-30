"""Streams app — Streams, StreamPlatforms, StreamSessions (§17), restream orchestration (§3/§5)."""
from django.apps import AppConfig


class StreamsConfig(AppConfig):
    name = "apps.streams"
    label = "streams"
    verbose_name = "Live Streams"
