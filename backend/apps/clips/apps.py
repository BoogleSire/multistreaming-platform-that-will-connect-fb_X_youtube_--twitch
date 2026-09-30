"""Clips Studio app config (§17 entity home for StreamClip)."""
from django.apps import AppConfig


class ClipsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.clips"
    verbose_name = "Clips Studio"
