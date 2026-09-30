"""Accounts app — custom user, profiles, sessions/devices, 2FA, verification (§16, §17)."""
from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    label = "accounts"
    verbose_name = "Accounts & Authentication"
