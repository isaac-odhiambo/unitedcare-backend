from rest_framework import serializers
from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    sender_name = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id",
            "title",
            "message",
            "notification_type",
            "action_url",
            "loan_id",
            "merry_id",
            "group_id",
            "is_read",
            "is_deleted",
            "sender_name",
            "created_at",
            "read_at",
        ]

    def get_sender_name(self, obj):
        if not obj.created_by:
            return "System"

        for attr in ("full_name", "get_full_name", "username", "phone", "email"):
            value = getattr(obj.created_by, attr, None)
            if callable(value):
                value = value()
            if value:
                return value
        return "Admin"


class CreateNotificationSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    title = serializers.CharField(max_length=150)
    message = serializers.CharField()
    notification_type = serializers.ChoiceField(
        choices=["INFO", "SUCCESS", "WARNING", "ERROR", "ACTION"],
        default="INFO",
    )
    action_url = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    loan_id = serializers.IntegerField(required=False, allow_null=True)
    merry_id = serializers.IntegerField(required=False, allow_null=True)
    group_id = serializers.IntegerField(required=False, allow_null=True)