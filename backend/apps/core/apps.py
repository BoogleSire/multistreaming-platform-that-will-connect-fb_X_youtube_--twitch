"""Core shared utilities: encrypted fields, RBAC permissions, errors, middleware, logging."""
from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"
    verbose_name = "StreamForge Core"
