# groups/serializers.py
from django.contrib.auth import get_user_model
from rest_framework import serializers
from .models import Group, GroupMembership

User = get_user_model()


class UserPublicSerializer(serializers.ModelSerializer):
    """
    ✅ Safe subset for group/membership listings.
    Adjust fields to match your custom user model.
    """
    class Meta:
        model = User
        fields = ["id", "username", "phone"]  # add "status", "role" if you want


class GroupSerializer(serializers.ModelSerializer):
    members_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Group
        fields = ["id", "name", "created_at", "members_count"]


class GroupMembershipSerializer(serializers.ModelSerializer):
    user = UserPublicSerializer(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(
        source="user",
        queryset=User.objects.all(),
        write_only=True,
        required=True,
    )
    group_id = serializers.PrimaryKeyRelatedField(
        source="group",
        queryset=Group.objects.all(),
        write_only=True,
        required=True,
    )

    class Meta:
        model = GroupMembership
        fields = [
            "id",
            "group",
            "group_id",
            "user",
            "user_id",
            "role",
            "is_active",
            "joined_at",
        ]
        read_only_fields = ["id", "group", "user", "joined_at"]

    def validate(self, attrs):
        # optional extra validation
        group = attrs.get("group") or getattr(self.instance, "group", None)
        user = attrs.get("user") or getattr(self.instance, "user", None)

        if group and user:
            exists = GroupMembership.objects.filter(group=group, user=user).exists()
            if not self.instance and exists:
                raise serializers.ValidationError("User is already a member of this group.")
        return attrs