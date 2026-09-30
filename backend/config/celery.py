"""Celery application (§18). Workers import this."""
import os

from celery import Celery
from celery.signals import setup_logging

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("streamforge")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["apps.platforms", "apps.streams", "apps.comments", "apps.ai",
                        "apps.moderation", "apps.devices", "apps.notifications"])


@setup_logging.connect
def _configure_logging(**kwargs):  # pragma: no cover
    import logging.config

    from django.conf import settings

    logging.config.dictConfig(settings.LOGGING)
