# groups/serializers.py
from decimal import Decimal

from rest_framework import serializers

from .models import (
    Group,
    GroupContribution,
    GroupFund,
    GroupJoinRequest,
    GroupMemberShare,
    GroupMembership,
)


# ---------------------------------------------------
# Group
# ---------------------------------------------------
class GroupSerializer(serializers.ModelSerializer):
    group_type_display = serializers.CharField(source="get_group_type_display", read_only=True)
    available_slots = serializers.SerializerMethodField()
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Group
        fields = (
            "id",
            "name",
            "payment_code",
            "group_type",
            "group_type_display",
            "description",
            "objective",
            "created_by",
            "visibility",
            "join_policy",
            "is_active",
            "max_members",
            "available_slots",
            "member_count",
            "requires_contributions",
            "contribution_amount",
            "contribution_frequency",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "created_by",
            "created_at",
            "updated_at",
            "group_type_display",
            "available_slots",
            "member_count",
        )

    def get_available_slots(self, obj):
        return obj.available_slots()

    def get_member_count(self, obj):
        return obj.memberships.filter(is_active=True).count()

    def validate_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Group name is required.")
        return value

    def validate_max_members(self, value):
        if value is None:
            return 0
        if value < 0:
            raise serializers.ValidationError("max_members cannot be negative.")
        return value

    def validate_payment_code(self, value):
        value = (value or "").strip().upper()
        if not value:
            return value

        if not value.isalpha():
            raise serializers.ValidationError(
                "payment_code must contain letters only, e.g. UN, WF, MG."
            )
        return value

    def validate(self, attrs):
        requires_contributions = attrs.get(
            "requires_contributions",
            getattr(self.instance, "requires_contributions", False) if self.instance else False,
        )
        contribution_amount = attrs.get(
            "contribution_amount",
            getattr(self.instance, "contribution_amount", Decimal("0.00")) if self.instance else Decimal("0.00"),
        )

        if requires_contributions and Decimal(str(contribution_amount or "0")) <= 0:
            raise serializers.ValidationError(
                {"contribution_amount": "Contribution amount must be greater than 0 when contributions are required."}
            )

        return attrs


# ---------------------------------------------------
# Membership
# ---------------------------------------------------
class GroupMembershipSerializer(serializers.ModelSerializer):
    group_name = serializers.CharField(source="group.name", read_only=True)
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = GroupMembership
        fields = (
            "id",
            "group",
            "group_name",
            "user",
            "user_name",
            "role",
            "is_active",
            "joined_at",
        )
        read_only_fields = ("id", "joined_at", "group_name", "user_name")

    def get_user_name(self, obj):
        return (
            getattr(obj.user, "username", None)
            or getattr(obj.user, "full_name", None)
            or getattr(obj.user, "name", None)
            or str(obj.user_id)
        )

    def validate_role(self, value):
        allowed = {"MEMBER", "ADMIN", "TREASURER", "SECRETARY"}
        value = (value or "").strip().upper()
        if value not in allowed:
            raise serializers.ValidationError("role must be MEMBER, ADMIN, TREASURER or SECRETARY.")
        return value


# ---------------------------------------------------
# Join Request
# ---------------------------------------------------
class GroupJoinRequestSerializer(serializers.ModelSerializer):
    group_name = serializers.CharField(source="group.name", read_only=True)
    user_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = GroupJoinRequest
        fields = (
            "id",
            "group",
            "group_name",
            "user",
            "user_name",
            "note",
            "status",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "created_at",
        )
        read_only_fields = (
            "id",
            "status",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "created_at",
            "group_name",
            "user_name",
        )

    def get_user_name(self, obj):
        return (
            getattr(obj.user, "username", None)
            or getattr(obj.user, "full_name", None)
            or getattr(obj.user, "name", None)
            or str(obj.user_id)
        )

    def get_reviewed_by_name(self, obj):
        if not obj.reviewed_by:
            return None
        return (
            getattr(obj.reviewed_by, "username", None)
            or getattr(obj.reviewed_by, "full_name", None)
            or getattr(obj.reviewed_by, "name", None)
            or str(obj.reviewed_by_id)
        )

    def validate_note(self, value):
        return (value or "").strip()


# ---------------------------------------------------
# Fund
# ---------------------------------------------------
class GroupFundSerializer(serializers.ModelSerializer):
    available_balance = serializers.CharField(read_only=True)

    class Meta:
        model = GroupFund
        fields = (
            "group",
            "balance",
            "reserved_amount",
            "available_balance",
            "created_at",
        )
        read_only_fields = ("created_at", "available_balance")


# ---------------------------------------------------
# Member Share
# ---------------------------------------------------
class GroupMemberShareSerializer(serializers.ModelSerializer):
    available_share = serializers.CharField(read_only=True)

    class Meta:
        model = GroupMemberShare
        fields = (
            "group",
            "user",
            "total_contributed",
            "reserved_share",
            "available_share",
            "updated_at",
        )
        read_only_fields = ("updated_at", "available_share")


# ---------------------------------------------------
# Contribution
# ---------------------------------------------------
class GroupContributionSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = GroupContribution
        fields = (
            "id",
            "group",
            "user",
            "user_name",
            "amount",
            "source",
            "reference",
            "note",
            "created_at",
        )
        read_only_fields = ("id", "created_at", "user_name")

    def get_user_name(self, obj):
        return (
            getattr(obj.user, "username", None)
            or getattr(obj.user, "full_name", None)
            or getattr(obj.user, "name", None)
            or str(obj.user_id)
        )


# ---------------------------------------------------
# Post Contribution Input
# ---------------------------------------------------
class PostContributionSerializer(serializers.Serializer):
    group_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2)
    source = serializers.CharField(required=False, allow_blank=True, default="MANUAL")
    reference = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)

    def validate_group_id(self, value):
        if value <= 0:
            raise serializers.ValidationError("group_id must be greater than 0.")
        return value

    def validate_amount(self, value):
        if value is None or Decimal(str(value)) <= 0:
            raise serializers.ValidationError("Amount must be greater than 0.")
        return value

    def validate_source(self, value):
        value = (value or "MANUAL").strip().upper()
        allowed = {"MANUAL", "MPESA", "BANK", "OTHER"}
        if value not in allowed:
            raise serializers.ValidationError("source must be MANUAL, MPESA, BANK or OTHER.")
        return value

    def validate_reference(self, value):
        return (value or "").strip()

    def validate_note(self, value):
        return (value or "").strip()