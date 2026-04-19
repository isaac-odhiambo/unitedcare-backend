from rest_framework import serializers

from .models import Channel, ChannelPost


class ChannelSerializer(serializers.ModelSerializer):
    group_name = serializers.SerializerMethodField()

    class Meta:
        model = Channel
        fields = [
            "id",
            "name",
            "description",
            "channel_type",
            "group",
            "group_name",
            "is_active",
            "allow_member_submissions",
            "created_at",
        ]

    def get_group_name(self, obj):
        if obj.group:
            return getattr(obj.group, "name", None)
        return None


class ChannelPostSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ChannelPost
        fields = [
            "id",
            "channel",
            "user",
            "user_name",
            "title",
            "content",
            "message_type",
            "status",
            "is_pinned",
            "approved_by",
            "approved_by_name",
            "approved_at",
            "rejection_reason",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "user",
            "status",
            "is_pinned",
            "approved_by",
            "approved_by_name",
            "approved_at",
            "rejection_reason",
            "created_at",
            "updated_at",
        ]

    def get_user_name(self, obj):
        if hasattr(obj.user, "get_full_name"):
            full_name = obj.user.get_full_name()
            if full_name:
                return full_name
        return getattr(obj.user, "username", "Member")

    def get_approved_by_name(self, obj):
        if not obj.approved_by:
            return None
        if hasattr(obj.approved_by, "get_full_name"):
            full_name = obj.approved_by.get_full_name()
            if full_name:
                return full_name
        return getattr(obj.approved_by, "username", "Admin")