"""AES-256-GCM envelope encryption for OAuth tokens at rest (§17, §30).

The data key is derived from TOKEN_ENCRYPTION_KEY (base64 of 32 random bytes).
Ciphertext format: base64( nonce(12) || tag(16) || ciphertext ).
Plaintext never touches the DB; decryption happens only inside integration services.
"""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


def _aesgcm() -> AESGCM:
    key_b64 = getattr(settings, "TOKEN_ENCRYPTION_KEY", "")
    if not key_b64:
        raise ImproperlyConfigured(
            "TOKEN_ENCRYPTION_KEY is not configured. Generate one with: "
            "python -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\""
        )
    try:
        key = base64.b64decode(key_b64, validate=True)
    except Exception as exc:  # pragma: no cover
        raise ImproperlyConfigured(f"TOKEN_ENCRYPTION_KEY is not valid base64: {exc}")
    if len(key) != 32:
        raise ImproperlyConfigured("TOKEN_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return AESGCM(key)


def encrypt(plaintext: str) -> str:
    if plaintext is None:
        return ""
    nonce = os.urandom(12)
    ct = _aesgcm().encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    # ct already contains ciphertext||tag
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    raw = base64.b64decode(token)
    nonce, ct = raw[:12], raw[12:]
    return _aesgcm().decrypt(nonce, ct, associated_data=None).decode("utf-8")


class EncryptedTextField(models.TextField):
    """TextField that transparently encrypts on save and decrypts on load.

    Note: encrypted values are not queryable/lookup-able by design — that is a
    feature (token material must never be searchable in the database layer).
    """

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value in (None, ""):
            return value
        # Avoid double-encrypting an already-encrypted string loaded from DB.
        if isinstance(value, str) and value.startswith("enc:v1:"):
            return value
        return "enc:v1:" + encrypt(value)

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        if value.startswith("enc:v1:"):
            return decrypt(value[len("enc:v1:"):])
        return value


class EncryptedJSONField(models.JSONField):
    """JSONField stored as encrypted text."""

    def get_prep_value(self, value):
        import json as _json

        prepared = super().get_prep_value(value)
        if prepared in (None, ""):
            return prepared
        if isinstance(prepared, str) and prepared.startswith("enc:v1:"):
            return prepared
        return "enc:v1:" + encrypt(prepared)

    def from_db_value(self, value, expression, connection):
        import json as _json

        if value is None:
            return None
        if isinstance(value, str) and value.startswith("enc:v1:"):
            value = decrypt(value[len("enc:v1:"):])
        if isinstance(value, str):
            try:
                return _json.loads(value)
            except ValueError:
                return value
        return value
