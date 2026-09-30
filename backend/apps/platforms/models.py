"""Connected platform accounts and their encrypted OAuth tokens (§17).

Security: access/refresh tokens are stored ONLY through EncryptedTextField (AES-256-GCM).
Serializers never expose them; the API exposes status booleans and public account metadata.
"""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.crypto import EncryptedTextField
from apps.core.models import TimeStampedModel


class ConnectedPlatform(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="connected_platforms")
    platform = models.CharField(max_length=32, db_index=True)      # registry key
    external_account_id = models.CharField(max_length=128, blank=True)
    account_name = models.CharField(max_length=200, blank=True)
    account_avatar_url = models.URLField(blank=True, null=True)
    scopes = models.TextField(blank=True)
    status = models.CharField(max_length=20, default="connected",
                              choices=[("connected", "Connected"), ("expired", "Authorization expired"),
                                       ("error", "Error"), ("disconnected", "Disconnected")])
    last_error = models.TextField(blank=True)                       # human message shown in UI
    extra = models.JSONField(default=dict, blank=True)              # e.g. has_live_scope, manual key flag
    connected_at = models.DateTimeField(default=timezone.now)
    disconnected_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "platform", "external_account_id"],
                                    name="uniq_user_platform_account"),
        ]
        indexes = [models.Index(fields=["user", "platform"])]

    def __str__(self):
        return f"{self.user}:{self.platform} ({self.account_name})"

    @property
    def token(self) -> "PlatformToken | None":
        return getattr(self, "platform_token", None)


class PlatformToken(TimeStampedModel):
    """OAuth material for one ConnectedPlatform row. Encrypted at rest (§17)."""

    connected = models.OneToOneField(ConnectedPlatform, on_delete=models.CASCADE,
                                     related_name="platform_token")
    access_token_enc = EncryptedTextField()
    refresh_token_enc = EncryptedTextField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    # adapter-private bits (PKCE verifier leftovers, page/user sub-tokens) also encrypted
    secret_blob_enc = EncryptedTextField(blank=True)

    @property
    def near_expiry(self) -> bool:
        if not self.expires_at:
            return False
        return self.expires_at <= timezone.now() + timezone.timedelta(hours=24)
