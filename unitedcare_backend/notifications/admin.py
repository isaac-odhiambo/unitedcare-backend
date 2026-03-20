from django.contrib import admin
from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "created_by",
        "title",
        "notification_type",
        "is_read",
        "is_deleted",
        "created_at",
    )
    list_filter = (
        "notification_type",
        "is_read",
        "is_deleted",
        "created_at",
    )
    search_fields = (
        "title",
        "message",
        "user__username",
        "user__email",
        "created_by__username",
        "created_by__email",
    )
    readonly_fields = ("created_at", "read_at")