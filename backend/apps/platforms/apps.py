"""Platforms app — ConnectedPlatforms + encrypted PlatformTokens (§2, §17, §30)."""
from django.apps import AppConfig


class PlatformsConfig(AppConfig):
    name = "apps.platforms"
    label = "platforms"
    verbose_name = "Connected Platforms"
