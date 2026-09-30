"""Serializers for registration, login (with 2FA), profile & sessions (§16)."""
from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import Session, User


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=10, style={"input_type": "password"})
    password_confirm = serializers.CharField(write_only=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ["full_name", "username", "email", "phone", "password", "password_confirm"]
        extra_kwargs = {"full_name": {"required": True}, "phone": {"required": False}}

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("That username is taken.")
        return value

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists. "
                                              "Use the password reset link if you forgot it.")
        return value.lower()

    def validate_password(self, value):
        validate_password(value)   # Django validators: length/common/numeric
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError({"password": "Passwords do not match."})
        return attrs

    def create(self, validated_data):
        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data["email"],
            password=validated_data["password"],
            full_name=validated_data.get("full_name", ""),
            phone=validated_data.get("phone", ""),
        )
        return user


class UserSerializer(serializers.ModelSerializer):
    """Public-safe representation — never includes password hashes or secrets."""

    display_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "email", "full_name", "display_name", "phone",
                  "profile_image", "email_verified", "totp_enabled", "plan", "date_joined"]
        read_only_fields = ["id", "username", "email", "email_verified", "totp_enabled",
                            "plan", "date_joined"]


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    otp_token = serializers.CharField(required=False, allow_blank=True, max_length=8)

    def validate(self, attrs):
        user = authenticate(request=self.context.get("request"),
                            username=attrs["email"], password=attrs["password"])
        if user is None:
            raise serializers.ValidationError(
                {"detail": "Email or password is incorrect. Need a new password? Use 'Forgot password'."})
        if not user.is_active:
            raise serializers.ValidationError({"detail": "This account has been deactivated."})
        if user.totp_enabled:
            token = attrs.get("otp_token", "")
            if not token:
                raise serializers.ValidationError({"otp_required": True,
                                                   "detail": "Two-factor code required."})
            from .services import verify_totp

            if not verify_totp(user, token):
                raise serializers.ValidationError({"otp_token": "Invalid or expired two-factor code."})
        attrs["user"] = user
        return attrs


class SessionSerializer(serializers.ModelSerializer):
    active = serializers.BooleanField(read_only=True)

    class Meta:
        model = Session
        fields = ["id", "device_label", "platform", "ip_address", "created_at",
                  "last_seen_at", "expires_at", "revoked_at", "active"]


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=64)
    new_password = serializers.CharField(min_length=10)

    def validate_new_password(self, value):
        validate_password(value)
        return value


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=10)

    def validate_current_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        validate_password(value, user=self.context["request"].user)
        return value
