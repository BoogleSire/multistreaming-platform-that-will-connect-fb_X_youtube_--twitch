from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import Session, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ["-date_joined"]
    list_display = ["username", "email", "full_name", "plan", "email_verified", "totp_enabled", "is_active"]
    readonly_fields = ["password", "created_at", "updated_at"]
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("StreamForge", {"fields": ("full_name", "phone", "profile_image", "email_verified",
                                     "totp_enabled", "plan")}),
    )


admin.site.register(Session)
