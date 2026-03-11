from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html

from .models import User, OTP, KYCProfile


# =========================================================
# USER ADMIN ACTIONS
# =========================================================
@admin.action(description="Approve selected users for full app access")
def approve_users(modeladmin, request, queryset):
    updated = queryset.exclude(status="blocked").update(status="approved")
    modeladmin.message_user(
        request,
        f"{updated} user(s) approved successfully.",
        level=messages.SUCCESS,
    )


@admin.action(description="Mark selected users as pending")
def mark_users_pending(modeladmin, request, queryset):
    updated = queryset.exclude(status="blocked").update(status="pending")
    modeladmin.message_user(
        request,
        f"{updated} user(s) moved to pending.",
        level=messages.WARNING,
    )


@admin.action(description="Block selected users")
def block_users(modeladmin, request, queryset):
    updated = queryset.update(status="blocked", is_active=False)
    modeladmin.message_user(
        request,
        f"{updated} user(s) blocked successfully.",
        level=messages.WARNING,
    )


@admin.action(description="Unblock selected users")
def unblock_users(modeladmin, request, queryset):
    updated = queryset.filter(status="blocked").update(status="pending", is_active=True)
    modeladmin.message_user(
        request,
        f"{updated} user(s) unblocked and set to pending.",
        level=messages.SUCCESS,
    )


@admin.action(description="Set selected users as members")
def make_members(modeladmin, request, queryset):
    count = 0
    for user in queryset:
        if not user.is_superuser:
            user.role = "member"
            user.is_staff = False
            user.save(update_fields=["role", "is_staff"])
            count += 1

    modeladmin.message_user(
        request,
        f"{count} user(s) set as members.",
        level=messages.SUCCESS,
    )


@admin.action(description="Set selected users as admins")
def make_admins(modeladmin, request, queryset):
    count = 0
    for user in queryset:
        if not user.is_superuser:
            user.role = "admin"
            user.is_staff = True
            user.save(update_fields=["role", "is_staff"])
            count += 1

    modeladmin.message_user(
        request,
        f"{count} user(s) set as admins.",
        level=messages.SUCCESS,
    )


@admin.action(description="Unlock selected user accounts")
def unlock_users(modeladmin, request, queryset):
    updated = 0
    for user in queryset:
        if user.locked_until or user.failed_login_attempts > 0:
            user.locked_until = None
            user.failed_login_attempts = 0
            user.save(update_fields=["locked_until", "failed_login_attempts"])
            updated += 1

    modeladmin.message_user(
        request,
        f"{updated} user account(s) unlocked.",
        level=messages.SUCCESS,
    )


# =========================================================
# KYC ADMIN ACTIONS
# =========================================================
@admin.action(description="Approve selected KYC profiles")
def approve_kyc(modeladmin, request, queryset):
    updated = 0
    for kyc in queryset:
        if kyc.status != "approved":
            kyc.status = "approved"
            kyc.save(update_fields=["status"])

        user = kyc.user
        if user.status != "blocked" and user.status != "approved":
            user.status = "approved"
            user.save(update_fields=["status"])

        updated += 1

    modeladmin.message_user(
        request,
        f"{updated} KYC profile(s) approved successfully.",
        level=messages.SUCCESS,
    )


@admin.action(description="Reject selected KYC profiles")
def reject_kyc(modeladmin, request, queryset):
    updated = queryset.update(status="rejected")
    modeladmin.message_user(
        request,
        f"{updated} KYC profile(s) rejected.",
        level=messages.WARNING,
    )


@admin.action(description="Mark selected KYC profiles as submitted")
def mark_kyc_submitted(modeladmin, request, queryset):
    updated = queryset.update(status="submitted")
    modeladmin.message_user(
        request,
        f"{updated} KYC profile(s) marked as submitted.",
        level=messages.SUCCESS,
    )


# =========================================================
# INLINE: KYC ON USER PAGE
# =========================================================
class KYCProfileInline(admin.StackedInline):
    model = KYCProfile
    extra = 0
    can_delete = False

    fields = (
        "status",
        "submitted_at",
        "passport_photo",
        "passport_photo_preview",
        "id_front",
        "id_front_preview",
        "id_back",
        "id_back_preview",
    )

    readonly_fields = (
        "submitted_at",
        "passport_photo_preview",
        "id_front_preview",
        "id_back_preview",
    )

    def passport_photo_preview(self, obj):
        if obj and obj.passport_photo:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="max-height:120px; max-width:120px; border-radius:8px; border:1px solid #ddd;" />'
                "</a>",
                obj.passport_photo.url,
            )
        return "No passport photo uploaded"

    passport_photo_preview.short_description = "Passport Preview"

    def id_front_preview(self, obj):
        if obj and obj.id_front:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="max-height:140px; max-width:220px; border-radius:8px; border:1px solid #ddd;" />'
                "</a>",
                obj.id_front.url,
            )
        return "No ID front uploaded"

    id_front_preview.short_description = "ID Front Preview"

    def id_back_preview(self, obj):
        if obj and obj.id_back:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="max-height:140px; max-width:220px; border-radius:8px; border:1px solid #ddd;" />'
                "</a>",
                obj.id_back.url,
            )
        return "No ID back uploaded"

    id_back_preview.short_description = "ID Back Preview"


# =========================================================
# USER ADMIN
# =========================================================
@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        "id",
        "phone",
        "username",
        "role",
        "status",
        "is_active",
        "is_staff",
        "is_superuser",
        "kyc_status_display",
        "has_full_access_display",
        "failed_login_attempts",
        "locked_until",
        "date_joined",
    )

    list_filter = (
        "role",
        "status",
        "is_active",
        "is_staff",
        "is_superuser",
        "date_joined",
    )

    search_fields = (
        "phone",
        "username",
        "email",
        "id_number",
    )

    ordering = ("-id",)

    readonly_fields = (
        "date_joined",
        "last_login",
    )

    actions = [
        approve_users,
        mark_users_pending,
        block_users,
        unblock_users,
        make_members,
        make_admins,
        unlock_users,
    ]

    inlines = [KYCProfileInline]

    fieldsets = (
        ("Login Credentials", {
            "fields": ("phone", "password")
        }),
        ("Personal Information", {
            "fields": ("username", "email", "id_number")
        }),
        ("Access Control", {
            "fields": (
                "role",
                "status",
                "is_active",
                "is_staff",
                "is_superuser",
                "groups",
                "user_permissions",
            )
        }),
        ("Security", {
            "fields": (
                "failed_login_attempts",
                "locked_until",
                "last_login",
                "date_joined",
            )
        }),
    )

    add_fieldsets = (
        ("Create User", {
            "classes": ("wide",),
            "fields": (
                "phone",
                "username",
                "email",
                "id_number",
                "role",
                "status",
                "is_active",
                "is_staff",
                "is_superuser",
                "password1",
                "password2",
            ),
        }),
    )

    def kyc_status_display(self, obj):
        return obj.kyc_status

    kyc_status_display.short_description = "KYC Status"

    def has_full_access_display(self, obj):
        return obj.has_full_access

    has_full_access_display.boolean = True
    has_full_access_display.short_description = "Full Access"

    def get_readonly_fields(self, request, obj=None):
        readonly = list(self.readonly_fields)
        if not request.user.is_superuser:
            readonly.extend(["is_superuser"])
        return readonly

    def save_model(self, request, obj, form, change):
        if obj.role == "admin":
            obj.is_staff = True
        elif obj.role == "member" and not obj.is_superuser:
            obj.is_staff = False

        if obj.status == "blocked":
            obj.is_active = False

        super().save_model(request, obj, form, change)


# =========================================================
# OTP ADMIN
# =========================================================
@admin.register(OTP)
class OTPAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "phone",
        "code",
        "is_used",
        "attempts",
        "is_expired_display",
        "is_locked_display",
        "created_at",
    )

    list_filter = (
        "is_used",
        "created_at",
    )

    search_fields = (
        "phone",
        "code",
    )

    ordering = ("-created_at",)

    readonly_fields = (
        "phone",
        "code",
        "created_at",
        "attempts",
        "is_used",
    )

    def is_expired_display(self, obj):
        return obj.is_expired()

    is_expired_display.boolean = True
    is_expired_display.short_description = "Expired"

    def is_locked_display(self, obj):
        return obj.is_locked()

    is_locked_display.boolean = True
    is_locked_display.short_description = "Locked"

    def has_add_permission(self, request):
        return False


# =========================================================
# KYC PROFILE ADMIN
# =========================================================
@admin.register(KYCProfile)
class KYCProfileAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "user_phone",
        "status",
        "passport_thumb",
        "id_front_thumb",
        "id_back_thumb",
        "submitted_at",
    )

    list_filter = (
        "status",
        "submitted_at",
    )

    search_fields = (
        "user__phone",
        "user__username",
        "user__id_number",
    )

    ordering = ("-submitted_at",)

    readonly_fields = (
        "submitted_at",
        "passport_photo_preview",
        "id_front_preview",
        "id_back_preview",
    )

    actions = [
        approve_kyc,
        reject_kyc,
        mark_kyc_submitted,
    ]

    fieldsets = (
        ("Owner", {
            "fields": ("user",)
        }),
        ("Documents", {
            "fields": (
                "passport_photo",
                "passport_photo_preview",
                "id_front",
                "id_front_preview",
                "id_back",
                "id_back_preview",
            )
        }),
        ("Review", {
            "fields": ("status", "submitted_at")
        }),
    )

    def user_phone(self, obj):
        return obj.user.phone

    user_phone.short_description = "Phone"

    def passport_thumb(self, obj):
        if obj.passport_photo:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="height:50px; width:50px; object-fit:cover; border-radius:6px; border:1px solid #ddd;" />'
                "</a>",
                obj.passport_photo.url,
            )
        return "-"

    passport_thumb.short_description = "Passport"

    def id_front_thumb(self, obj):
        if obj.id_front:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="height:50px; width:80px; object-fit:cover; border-radius:6px; border:1px solid #ddd;" />'
                "</a>",
                obj.id_front.url,
            )
        return "-"

    id_front_thumb.short_description = "ID Front"

    def id_back_thumb(self, obj):
        if obj.id_back:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="height:50px; width:80px; object-fit:cover; border-radius:6px; border:1px solid #ddd;" />'
                "</a>",
                obj.id_back.url,
            )
        return "-"

    id_back_thumb.short_description = "ID Back"

    def passport_photo_preview(self, obj):
        if obj and obj.passport_photo:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="max-height:180px; max-width:180px; border-radius:8px; border:1px solid #ddd;" />'
                "</a>",
                obj.passport_photo.url,
            )
        return "No passport photo uploaded"

    passport_photo_preview.short_description = "Passport Preview"

    def id_front_preview(self, obj):
        if obj and obj.id_front:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="max-height:220px; max-width:320px; border-radius:8px; border:1px solid #ddd;" />'
                "</a>",
                obj.id_front.url,
            )
        return "No ID front uploaded"

    id_front_preview.short_description = "ID Front Preview"

    def id_back_preview(self, obj):
        if obj and obj.id_back:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="max-height:220px; max-width:320px; border-radius:8px; border:1px solid #ddd;" />'
                "</a>",
                obj.id_back.url,
            )
        return "No ID back uploaded"

    id_back_preview.short_description = "ID Back Preview"