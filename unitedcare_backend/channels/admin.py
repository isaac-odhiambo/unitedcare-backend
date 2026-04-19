from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import Channel, ChannelPost


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "channel_type",
        "group",
        "is_active",
        "allow_member_submissions",
        "created_at",
    )
    list_filter = ("channel_type", "is_active", "allow_member_submissions")
    search_fields = ("name", "description")


@admin.register(ChannelPost)
class ChannelPostAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "channel",
        "user",
        "message_type",
        "status",
        "is_pinned",
        "created_at",
    )
    list_filter = ("status", "message_type", "is_pinned", "channel")
    search_fields = ("title", "content", "user__username")