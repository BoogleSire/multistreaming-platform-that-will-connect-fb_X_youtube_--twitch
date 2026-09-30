"""User + auth models (§16, §17). Passwords are hashed (Argon2 first) — never plaintext."""
import secrets
import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


USERNAME_RE = RegexValidator(r"^[a-zA-Z0-9_.-]{3,30}$",
                             "Username may contain letters, numbers, . _ - and must be 3–30 chars.")


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, username, email, password, **extra):
        if not email:
            raise ValueError("Email is required")
        user = self.model(username=username, email=self.normalize_email(email), **extra)
        user.set_password(password)          # hashed via PASSWORD_HASHERS
        user.save(using=self._db)
        return user

    def create_user(self, username, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(username, email, password, **extra)

    def create_superuser(self, username, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if extra.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        return self._create_user(username, email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """Primary account record. `email` is unique and used for verification/reset."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(max_length=30, unique=True, validators=[USERNAME_RE])
    email = models.EmailField(unique=True, db_index=True)
    full_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    profile_image = models.ImageField(upload_to="avatars/", null=True, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    # email verification (§16)
    email_verified = models.BooleanField(default=False)
    verification_hash = models.CharField(max_length=64, blank=True)     # sha256 of single-use token
    verification_expires_at = models.DateTimeField(null=True, blank=True)

    # password reset (§16)
    reset_hash = models.CharField(max_length=64, blank=True)
    reset_expires_at = models.DateTimeField(null=True, blank=True)

    # two-factor (§16)
    totp_secret_enc = models.TextField(blank=True)   # encrypted TOTP seed
    totp_enabled = models.BooleanField(default=False)

    # subscription architecture placeholder (§41) — no billing enforced yet
    plan = models.CharField(max_length=20, default="free")

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    objects = UserManager()

    class Meta:
        indexes = [models.Index(fields=["username"]), models.Index(fields=["email"])]

    def __str__(self):
        return f"{self.username} <{self.email}>"

    @property
    def display_name(self):
        return self.full_name or self.username


class Session(models.Model):
    """Server-side session/device fingerprint for 'logout all devices' (§16, §30)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sessions")
    jti = models.CharField(max_length=64, unique=True, db_index=True)  # refresh-token id
    device_label = models.CharField(max_length=120, blank=True)
    platform = models.CharField(max_length=30, blank=True)   # web | android | unknown
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_seen_at"]

    @property
    def active(self):
        return self.revoked_at is None and self.expires_at > timezone.now()


class PasswordChangeToken(TimeStampedModel):
    """Single-use, hashed-at-rest tokens for email verification & password reset (§30)."""

    PURPOSES = [("verify_email", "Email verification"), ("password_reset", "Password reset")]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="tokens")
    purpose = models.CharField(max_length=20, choices=PURPOSES)
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    @staticmethod
    def new_token() -> tuple[str, str]:
        raw = secrets.token_urlsafe(32)
        import hashlib

        return raw, hashlib.sha256(raw.encode()).hexdigest()
