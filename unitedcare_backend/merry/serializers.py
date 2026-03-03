# merry/serializers.py
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.utils import timezone
from rest_framework import serializers

from .models import (
    MerryGoRound,
    MerryMember,
    MerryJoinRequest,
    MerryContribution,
    MerryPayout,
)


# -----------------------------
# Small utilities
# -----------------------------
def q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def parse_decimal(value, field_name: str) -> Decimal:
    """
    Safely parse a decimal from request data (str/int/float).
    DRF DecimalField is good, but this helper is useful in validate() sometimes.
    """
    try:
        d = Decimal(str(value))
        return q2(d)
    except (InvalidOperation, TypeError, ValueError):
        raise serializers.ValidationError({field_name: "Invalid decimal amount."})


# -----------------------------
# READ serializers (responses)
# -----------------------------
class MerryGoRoundSerializer(serializers.ModelSerializer):
    members_count = serializers.SerializerMethodField()
    total_pool = serializers.SerializerMethodField()

    class Meta:
        model = MerryGoRound
        fields = [
            "id",
            "name",
            "contribution_amount",
            "cycle_duration_weeks",
            "payout_order_type",
            "next_payout_date",
            "created_by",
            "created_at",
            "members_count",
            "total_pool",
        ]
        read_only_fields = ["id", "created_by", "created_at", "members_count", "total_pool"]

    def get_members_count(self, obj: MerryGoRound) -> int:
        return obj.members.filter(is_active=True).count()

    def get_total_pool(self, obj: MerryGoRound) -> str:
        # return as string to avoid JSON float issues
        return str(obj.total_pool())


class MerryMemberSerializer(serializers.ModelSerializer):
    username = serializers.SerializerMethodField()
    phone = serializers.SerializerMethodField()

    class Meta:
        model = MerryMember
        fields = [
            "id",
            "merry",
            "user",
            "username",
            "phone",
            "payout_position",
            "joined_at",
            "is_active",
        ]
        read_only_fields = fields

    def get_username(self, obj: MerryMember):
        return getattr(obj.user, "username", None)

    def get_phone(self, obj: MerryMember):
        return getattr(obj.user, "phone", None)


class MerryJoinRequestSerializer(serializers.ModelSerializer):
    merry_name = serializers.CharField(source="merry.name", read_only=True)

    class Meta:
        model = MerryJoinRequest
        fields = [
            "id",
            "merry",
            "merry_name",
            "user",
            "status",
            "note",
            "reviewed_by",
            "reviewed_at",
            "created_at",
        ]
        read_only_fields = ["id", "status", "reviewed_by", "reviewed_at", "created_at", "user"]


class MerryContributionSerializer(serializers.ModelSerializer):
    merry_id = serializers.IntegerField(source="member.merry_id", read_only=True)
    merry_name = serializers.CharField(source="member.merry.name", read_only=True)

    class Meta:
        model = MerryContribution
        fields = [
            "id",
            "member",
            "merry_id",
            "merry_name",
            "week_number",
            "amount",
            "status",
            "paid_at",
            "mpesa_receipt_number",
            "created_at",
        ]
        read_only_fields = ["id", "status", "paid_at", "mpesa_receipt_number", "created_at"]


class MerryPayoutSerializer(serializers.ModelSerializer):
    merry_name = serializers.CharField(source="merry.name", read_only=True)
    member_user_id = serializers.IntegerField(source="member.user_id", read_only=True)

    class Meta:
        model = MerryPayout
        fields = [
            "id",
            "merry",
            "merry_name",
            "member",
            "member_user_id",
            "week_number",
            "amount",
            "status",
            "paid_at",
            "notes",
            "created_at",
        ]
        read_only_fields = ["id", "status", "paid_at", "created_at"]


# -----------------------------
# WRITE serializers (inputs)
# -----------------------------
class CreateMerrySerializer(serializers.ModelSerializer):
    """
    POST: create merry
    Admin-only check should be done in the view permission, not in serializer.
    """

    class Meta:
        model = MerryGoRound
        fields = [
            "name",
            "contribution_amount",
            "cycle_duration_weeks",
            "payout_order_type",
            "next_payout_date",
        ]

    def validate_name(self, value: str):
        v = (value or "").strip()
        if not v:
            raise serializers.ValidationError("Name is required.")
        if len(v) < 3:
            raise serializers.ValidationError("Name is too short.")
        return v

    def validate_contribution_amount(self, value: Decimal):
        v = q2(value)
        if v <= 0:
            raise serializers.ValidationError("Contribution amount must be greater than 0.")
        return v

    def validate_cycle_duration_weeks(self, value: int):
        if value < 1 or value > 52:
            raise serializers.ValidationError("cycle_duration_weeks must be between 1 and 52.")
        return value

    def validate_payout_order_type(self, value: str):
        if value not in ("manual", "random"):
            raise serializers.ValidationError("payout_order_type must be 'manual' or 'random'.")
        return value

    def create(self, validated_data):
        user = self.context["request"].user
        validated_data["created_by"] = user
        return super().create(validated_data)


class JoinRequestCreateSerializer(serializers.Serializer):
    """
    Member requests to join a merry.
    POST body: { "note": "..." } (optional)
    Merry id comes from URL and should be injected in view.
    """
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        request = self.context["request"]
        merry: MerryGoRound = self.context["merry"]

        # Already a member?
        if MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists():
            raise serializers.ValidationError("You are already a member of this merry.")

        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        merry: MerryGoRound = self.context["merry"]

        note = (validated_data.get("note") or "").strip()[:255]

        # Reuse existing record if it exists
        existing = MerryJoinRequest.objects.filter(merry=merry, user=request.user).first()

        if existing:
            if existing.status == "PENDING":
                # Keep pending, just refresh note optionally
                if note and note != existing.note:
                    existing.note = note
                    existing.save(update_fields=["note"])
                return existing

            # allow resubmit after rejected/cancelled
            existing.status = "PENDING"
            existing.note = note
            existing.reviewed_by = None
            existing.reviewed_at = None
            existing.created_at = timezone.now()
            existing.full_clean()
            existing.save(update_fields=["status", "note", "reviewed_by", "reviewed_at", "created_at"])
            return existing

        jr = MerryJoinRequest(merry=merry, user=request.user, status="PENDING", note=note)
        jr.full_clean()
        jr.save()
        return jr


class JoinRequestCancelSerializer(serializers.Serializer):
    """
    Member cancels their own PENDING join request.
    No body required.
    """

    def validate(self, attrs):
        request = self.context["request"]
        jr: MerryJoinRequest = self.context["join_request"]

        if jr.user_id != request.user.id:
            raise serializers.ValidationError("You can only cancel your own join request.")
        if jr.status != "PENDING":
            raise serializers.ValidationError("Only PENDING requests can be cancelled.")
        return attrs

    def save(self, **kwargs):
        jr: MerryJoinRequest = self.context["join_request"]
        user = self.context["request"].user
        jr.cancel(user)
        return jr


class AdminApproveJoinRequestSerializer(serializers.Serializer):
    """
    Admin approves PENDING join request.
    No body required.
    """

    def validate(self, attrs):
        jr: MerryJoinRequest = self.context["join_request"]
        if jr.status != "PENDING":
            raise serializers.ValidationError("Only PENDING requests can be approved.")
        return attrs

    def save(self, **kwargs):
        jr: MerryJoinRequest = self.context["join_request"]
        admin_user = self.context["request"].user
        member = jr.approve(admin_user)  # auto payout assignment happens here (model method)
        return member


class AdminRejectJoinRequestSerializer(serializers.Serializer):
    """
    Admin rejects PENDING join request.
    Body: { "note": "reason" } (optional)
    """
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        jr: MerryJoinRequest = self.context["join_request"]
        if jr.status != "PENDING":
            raise serializers.ValidationError("Only PENDING requests can be rejected.")
        return attrs

    def save(self, **kwargs):
        jr: MerryJoinRequest = self.context["join_request"]
        admin_user = self.context["request"].user
        note = (self.validated_data.get("note") or "").strip()
        jr.reject(admin_user, note=note)
        return jr


class ContributionIntentSerializer(serializers.Serializer):
    """
    Member creates a contribution intent.
    POST body: { "week_number": 1 } optional
    Amount is derived from merry.contribution_amount.
    """
    week_number = serializers.IntegerField(required=False, min_value=1)

    def validate(self, attrs):
        request = self.context["request"]
        merry: MerryGoRound = self.context["merry"]

        # Must be an active member
        member = MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).first()
        if not member:
            raise serializers.ValidationError("You must be a member of this merry to contribute.")

        attrs["member"] = member

        # default week_number
        if "week_number" not in attrs or attrs["week_number"] is None:
            created = merry.created_at.date()
            today = timezone.now().date()
            delta_days = (today - created).days
            if delta_days < 0:
                delta_days = 0
            attrs["week_number"] = (delta_days // 7) + 1

        # prevent duplicates per member/week
        if MerryContribution.objects.filter(member=member, week_number=attrs["week_number"]).exists():
            raise serializers.ValidationError("Contribution already exists for this week.")

        return attrs

    def create(self, validated_data):
        member: MerryMember = validated_data["member"]
        week_number: int = validated_data["week_number"]
        merry = member.merry

        contribution = MerryContribution.objects.create(
            member=member,
            week_number=week_number,
            amount=merry.contribution_amount,
            status="PENDING",
        )
        return contribution


class CreatePayoutSerializer(serializers.Serializer):
    """
    Admin creates payout record (SCHEDULED).
    POST body:
      { "member_id": 12, "amount": 5000, "week_number": 3, "notes": "..." }
    """
    member_id = serializers.IntegerField(min_value=1)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    week_number = serializers.IntegerField(required=False, min_value=1)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        merry: MerryGoRound = self.context["merry"]

        member = MerryMember.objects.filter(id=attrs["member_id"], merry=merry, is_active=True).first()
        if not member:
            raise serializers.ValidationError({"member_id": "Member not found in this merry."})

        amount = q2(attrs["amount"])
        if amount <= 0:
            raise serializers.ValidationError({"amount": "Amount must be greater than 0."})

        # default week_number (same simple logic)
        if "week_number" not in attrs or attrs["week_number"] is None:
            created = merry.created_at.date()
            today = timezone.now().date()
            delta_days = (today - created).days
            if delta_days < 0:
                delta_days = 0
            attrs["week_number"] = (delta_days // 7) + 1

        # enforce unique payout per merry/week
        if MerryPayout.objects.filter(merry=merry, week_number=attrs["week_number"]).exists():
            raise serializers.ValidationError("A payout already exists for this merry and week.")

        attrs["member"] = member
        attrs["amount"] = amount
        attrs["notes"] = (attrs.get("notes") or "").strip()[:255]
        return attrs

    def create(self, validated_data):
        merry: MerryGoRound = self.context["merry"]
        member: MerryMember = validated_data["member"]

        payout = MerryPayout.objects.create(
            merry=merry,
            member=member,
            week_number=validated_data["week_number"],
            amount=validated_data["amount"],
            status="SCHEDULED",
            notes=validated_data["notes"],
        )
        return payout