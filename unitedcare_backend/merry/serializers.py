# merry/serializers.py
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.utils import timezone
from rest_framework import serializers

from .models import (
    MerryGoRound,
    MerryMember,
    MerrySeat,
    MerryJoinRequest,
    MerrySlotConfig,
    MerryContributionDue,
    MerryPayment,
    MerryPaymentAllocation,
    MerryPayout,
    MerryWallet,
    MerryWalletTransaction,
)


# -----------------------------
# Small utilities
# -----------------------------
def q2(value: Decimal) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def parse_decimal(value, field_name: str) -> Decimal:
    try:
        d = Decimal(str(value))
        return q2(d)
    except (InvalidOperation, TypeError, ValueError):
        raise serializers.ValidationError({field_name: "Invalid decimal amount."})


def parse_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def current_period_key_for_merry(merry: MerryGoRound) -> str:
    """
    Must match MerryGoRound.current_period_key():
      - DAILY   => "YYYY-MM-DD"
      - WEEKLY  => "YYYY-Www"
      - MONTHLY => "YYYY-MM"
    """
    today = timezone.localdate()
    freq = (getattr(merry, "payout_frequency", None) or "WEEKLY").upper()

    if freq == "DAILY":
        return today.isoformat()

    if freq == "MONTHLY":
        return f"{today.year}-{today.month:02d}"

    iso_year, iso_week, _ = today.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def payouts_per_period(merry: MerryGoRound) -> int:
    """
    Compatibility helper.
    Active ROSCA flow uses one payout at a time.
    """
    if hasattr(merry, "effective_payouts_per_period"):
        return max(1, int(merry.effective_payouts_per_period() or 1))
    return 1


# -----------------------------
# READ serializers (responses)
# -----------------------------
class MerryGoRoundSerializer(serializers.ModelSerializer):
    members_count = serializers.SerializerMethodField()
    seats_count = serializers.SerializerMethodField()
    total_pool_per_slot = serializers.SerializerMethodField()
    total_pool_per_period = serializers.SerializerMethodField()
    available_seats = serializers.SerializerMethodField()

    class Meta:
        model = MerryGoRound
        fields = [
            "id",
            "name",
            "contribution_amount",
            "cycle_duration_weeks",
            "payout_order_type",
            "next_payout_date",
            "payout_frequency",
            "payouts_per_period",
            "is_open",
            "max_seats",
            "available_seats",
            "penalty_mode",
            "flat_penalty_amount",
            "daily_penalty_amount",
            "penalty_grace_days",
            "penalty_cap_amount",
            "created_by",
            "created_at",
            "members_count",
            "seats_count",
            "total_pool_per_slot",
            "total_pool_per_period",
        ]
        read_only_fields = [
            "id",
            "created_by",
            "created_at",
            "available_seats",
            "members_count",
            "seats_count",
            "total_pool_per_slot",
            "total_pool_per_period",
        ]

    def get_members_count(self, obj: MerryGoRound) -> int:
        return obj.members.filter(is_active=True).count()

    def get_seats_count(self, obj: MerryGoRound) -> int:
        return obj.seats.filter(is_active=True).count()

    def get_total_pool_per_slot(self, obj: MerryGoRound) -> str:
        if hasattr(obj, "total_pool_per_slot"):
            return str(obj.total_pool_per_slot())
        seats = obj.seats.filter(is_active=True).count()
        return str(Decimal(seats) * (obj.contribution_amount or Decimal("0")))

    def get_total_pool_per_period(self, obj: MerryGoRound) -> str:
        if hasattr(obj, "total_pool_per_period"):
            return str(obj.total_pool_per_period())
        seats = obj.seats.filter(is_active=True).count()
        return str(Decimal(seats) * (obj.contribution_amount or Decimal("0")))

    def get_available_seats(self, obj: MerryGoRound):
        if hasattr(obj, "available_seats"):
            return obj.available_seats()
        return None


class MerryMemberSerializer(serializers.ModelSerializer):
    username = serializers.SerializerMethodField()
    phone = serializers.SerializerMethodField()
    seats_count = serializers.SerializerMethodField()

    class Meta:
        model = MerryMember
        fields = [
            "id",
            "merry",
            "user",
            "username",
            "phone",
            "joined_at",
            "is_active",
            "seats_count",
        ]
        read_only_fields = fields

    def get_username(self, obj: MerryMember):
        return getattr(obj.user, "username", None)

    def get_phone(self, obj: MerryMember):
        return getattr(obj.user, "phone", None)

    def get_seats_count(self, obj: MerryMember) -> int:
        return obj.seats.filter(is_active=True).count()


class MerrySeatSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="member.user.username", read_only=True)
    phone = serializers.CharField(source="member.user.phone", read_only=True)
    joined_at = serializers.DateTimeField(source="member.joined_at", read_only=True)

    class Meta:
        model = MerrySeat
        fields = [
            "id",
            "merry",
            "member",
            "seat_no",
            "payout_position",
            "is_active",
            "created_at",
            "username",
            "phone",
            "joined_at",
        ]
        read_only_fields = fields


class MerryJoinRequestSerializer(serializers.ModelSerializer):
    merry_name = serializers.CharField(source="merry.name", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    phone = serializers.CharField(source="user.phone", read_only=True)

    class Meta:
        model = MerryJoinRequest
        fields = [
            "id",
            "merry",
            "merry_name",
            "user",
            "username",
            "phone",
            "status",
            "note",
            "requested_seats",
            "reviewed_by",
            "reviewed_at",
            "created_at",
        ]
        read_only_fields = ["id", "status", "reviewed_by", "reviewed_at", "created_at", "user"]


class MerrySlotConfigSerializer(serializers.ModelSerializer):
    """
    Legacy compatibility serializer.
    """
    weekday_name = serializers.CharField(source="get_weekday_display", read_only=True)

    class Meta:
        model = MerrySlotConfig
        fields = ["merry", "slot_no", "weekday", "weekday_name"]
        read_only_fields = ["weekday_name"]


class MerryContributionDueSerializer(serializers.ModelSerializer):
    seat_no = serializers.IntegerField(source="seat.seat_no", read_only=True)
    member_id = serializers.IntegerField(source="seat.member_id", read_only=True)
    user_id = serializers.IntegerField(source="seat.member.user_id", read_only=True)
    username = serializers.CharField(source="seat.member.user.username", read_only=True)
    phone = serializers.CharField(source="seat.member.user.phone", read_only=True)

    payout_id = serializers.IntegerField(source="payout.id", read_only=True)
    turn_no = serializers.IntegerField(source="payout.turn_no", read_only=True)
    cycle_no = serializers.IntegerField(source="payout.cycle_no", read_only=True)
    scheduled_date = serializers.DateField(source="payout.scheduled_date", read_only=True)

    base_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    penalty_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    days_overdue = serializers.IntegerField(read_only=True)

    outstanding = serializers.SerializerMethodField()

    class Meta:
        model = MerryContributionDue
        fields = [
            "id",
            "merry",
            "seat",
            "seat_no",
            "member_id",
            "user_id",
            "username",
            "phone",
            "payout",
            "payout_id",
            "turn_no",
            "cycle_no",
            "scheduled_date",
            "period_key",
            "slot_no",
            "base_amount",
            "penalty_amount",
            "due_amount",
            "paid_amount",
            "outstanding",
            "status",
            "due_date",
            "days_overdue",
            "is_advance_payable",
            "updated_at",
            "created_at",
        ]
        read_only_fields = fields

    def get_outstanding(self, obj: MerryContributionDue) -> str:
        if hasattr(obj, "outstanding"):
            return str(obj.outstanding())
        return str((obj.due_amount or Decimal("0")) - (obj.paid_amount or Decimal("0")))


class MerryPaymentSerializer(serializers.ModelSerializer):
    merry_name = serializers.CharField(source="merry.name", read_only=True)
    beneficiary_user_id = serializers.IntegerField(source="beneficiary_member.user_id", read_only=True)
    beneficiary_username = serializers.CharField(source="beneficiary_member.user.username", read_only=True)

    class Meta:
        model = MerryPayment
        fields = [
            "id",
            "merry",
            "merry_name",
            "beneficiary_member",
            "beneficiary_user_id",
            "beneficiary_username",
            "payer_phone",
            "amount",
            "status",
            "period_key",
            "paid_at",
            "mpesa_receipt_number",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "paid_at",
            "mpesa_receipt_number",
            "created_at",
            "merry_name",
            "beneficiary_user_id",
            "beneficiary_username",
        ]


class MerryPaymentAllocationSerializer(serializers.ModelSerializer):
    payment_amount = serializers.DecimalField(
        source="payment.amount",
        read_only=True,
        max_digits=12,
        decimal_places=2,
    )
    amount = serializers.DecimalField(
        source="amount_allocated",
        read_only=True,
        max_digits=12,
        decimal_places=2,
    )
    due_period_key = serializers.CharField(source="due.period_key", read_only=True)
    due_slot_no = serializers.IntegerField(source="due.slot_no", read_only=True)
    due_seat_no = serializers.IntegerField(source="due.seat.seat_no", read_only=True)
    due_turn_no = serializers.IntegerField(source="due.payout.turn_no", read_only=True)
    due_cycle_no = serializers.IntegerField(source="due.payout.cycle_no", read_only=True)

    class Meta:
        model = MerryPaymentAllocation
        fields = [
            "id",
            "payment",
            "payment_amount",
            "due",
            "due_period_key",
            "due_slot_no",
            "due_seat_no",
            "due_turn_no",
            "due_cycle_no",
            "amount_allocated",
            "amount",
            "created_at",
        ]
        read_only_fields = fields
        
class MerryPayoutSerializer(serializers.ModelSerializer):
    merry_name = serializers.CharField(source="merry.name", read_only=True)

    seat_no = serializers.IntegerField(source="seat.seat_no", read_only=True)
    member_id = serializers.IntegerField(source="seat.member_id", read_only=True)
    user_id = serializers.IntegerField(source="seat.member.user_id", read_only=True)
    username = serializers.CharField(source="seat.member.user.username", read_only=True)
    phone = serializers.CharField(source="seat.member.user.phone", read_only=True)

    class Meta:
        model = MerryPayout
        fields = [
            "id",
            "merry",
            "merry_name",
            "seat",
            "seat_no",
            "member_id",
            "user_id",
            "username",
            "phone",
            "turn_no",
            "cycle_no",
            "scheduled_date",
            "period_key",
            "slot_no",
            "amount",
            "status",
            "paid_at",
            "notes",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "paid_at",
            "created_at",
            "merry_name",
            "seat_no",
            "member_id",
            "user_id",
            "username",
            "phone",
        ]


class MerryWalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = MerryWallet
        fields = [
            "id",
            "user",
            "balance",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class MerryWalletTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = MerryWalletTransaction
        fields = [
            "id",
            "user",
            "tx_type",
            "amount",
            "balance_before",
            "balance_after",
            "reference",
            "narration",
            "mpesa_receipt_number",
            "created_at",
        ]
        read_only_fields = fields


# -----------------------------
# Dashboard / breakdown serializers
# -----------------------------
class MerryDueItemBreakdownSerializer(serializers.Serializer):
    due_id = serializers.IntegerField()
    payout_id = serializers.IntegerField(required=False, allow_null=True)
    turn_no = serializers.IntegerField(required=False, allow_null=True)
    cycle_no = serializers.IntegerField(required=False, allow_null=True)
    seat_no = serializers.IntegerField()
    period_key = serializers.CharField()
    due_date = serializers.DateField(allow_null=True)
    status = serializers.CharField()
    base_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    penalty_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    due_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    outstanding = serializers.DecimalField(max_digits=12, decimal_places=2)
    days_overdue = serializers.IntegerField(required=False)
    bucket = serializers.CharField(required=False)


class MerryPerGroupSummarySerializer(serializers.Serializer):
    merry_id = serializers.IntegerField()
    merry_name = serializers.CharField()
    seat_count = serializers.IntegerField()
    seat_numbers = serializers.ListField(child=serializers.IntegerField())
    amount_per_seat = serializers.DecimalField(max_digits=12, decimal_places=2)

    current_turn = serializers.DictField(required=False)

    overdue_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    current_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    next_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_due_now = serializers.DecimalField(max_digits=12, decimal_places=2)

    next_due_date = serializers.DateField(allow_null=True)
    next_due_rows_count = serializers.IntegerField(required=False)
    wallet_balance = serializers.DecimalField(max_digits=14, decimal_places=2)

    breakdown = MerryDueItemBreakdownSerializer(many=True, required=False)


class MyAllMerryDueSummarySerializer(serializers.Serializer):
    active_merries = serializers.IntegerField()
    total_seats = serializers.IntegerField()
    overdue_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    current_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    next_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_due_now = serializers.DecimalField(max_digits=12, decimal_places=2)
    wallet_balance = serializers.DecimalField(max_digits=14, decimal_places=2)
    items = MerryPerGroupSummarySerializer(many=True)


class MerryPaymentBreakdownSerializer(serializers.Serializer):
    merry_id = serializers.IntegerField()
    merry_name = serializers.CharField()
    seat_count = serializers.IntegerField()
    seat_numbers = serializers.ListField(child=serializers.IntegerField())
    amount_per_seat = serializers.DecimalField(max_digits=12, decimal_places=2)
    include_next = serializers.BooleanField()
    overdue_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    current_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    next_total = serializers.DecimalField(max_digits=12, decimal_places=2)
    next_due_date = serializers.DateField(allow_null=True)
    total_due_now = serializers.DecimalField(max_digits=12, decimal_places=2)
    wallet_balance = serializers.DecimalField(max_digits=14, decimal_places=2)
    net_required_now_after_wallet = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)
    selected_total = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    items = MerryDueItemBreakdownSerializer(many=True)


# -----------------------------
# WRITE serializers (inputs)
# -----------------------------
class CreateMerrySerializer(serializers.ModelSerializer):
    is_open = serializers.BooleanField(required=False, default=True)
    max_seats = serializers.IntegerField(required=False, min_value=0, default=0)

    penalty_mode = serializers.ChoiceField(
        choices=["NONE", "FLAT", "DAILY"],
        required=False,
        default="NONE",
    )
    flat_penalty_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        default=Decimal("0.00"),
    )
    daily_penalty_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        default=Decimal("0.00"),
    )
    penalty_grace_days = serializers.IntegerField(required=False, min_value=0, default=0)
    penalty_cap_amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        allow_null=True,
    )

    class Meta:
        model = MerryGoRound
        fields = [
            "name",
            "contribution_amount",
            "cycle_duration_weeks",
            "payout_order_type",
            "next_payout_date",
            "payout_frequency",
            "payouts_per_period",
            "is_open",
            "max_seats",
            "penalty_mode",
            "flat_penalty_amount",
            "daily_penalty_amount",
            "penalty_grace_days",
            "penalty_cap_amount",
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
        if value < 1 or value > 520:
            raise serializers.ValidationError("cycle_duration_weeks must be between 1 and 520.")
        return value

    def validate_payout_order_type(self, value: str):
        if value not in ("manual", "random"):
            raise serializers.ValidationError("payout_order_type must be 'manual' or 'random'.")
        return value

    def validate_payout_frequency(self, value: str):
        v = (value or "WEEKLY").upper()
        if v not in ("DAILY", "WEEKLY", "MONTHLY"):
            raise serializers.ValidationError("payout_frequency must be 'DAILY', 'WEEKLY' or 'MONTHLY'.")
        return v

    def validate_payouts_per_period(self, value: int):
        if value < 1:
            raise serializers.ValidationError("payouts_per_period must be at least 1.")
        return 1

    def validate_max_seats(self, value: int):
        if value < 0:
            raise serializers.ValidationError("max_seats cannot be negative.")
        return value

    def validate_flat_penalty_amount(self, value: Decimal):
        v = q2(value)
        if v < 0:
            raise serializers.ValidationError("flat_penalty_amount cannot be negative.")
        return v

    def validate_daily_penalty_amount(self, value: Decimal):
        v = q2(value)
        if v < 0:
            raise serializers.ValidationError("daily_penalty_amount cannot be negative.")
        return v

    def validate_penalty_cap_amount(self, value):
        if value in (None, ""):
            return None
        v = q2(value)
        if v < 0:
            raise serializers.ValidationError("penalty_cap_amount cannot be negative.")
        return v

    def validate(self, attrs):
        penalty_mode = (attrs.get("penalty_mode") or "NONE").upper()
        flat_penalty_amount = q2(attrs.get("flat_penalty_amount") or Decimal("0.00"))
        daily_penalty_amount = q2(attrs.get("daily_penalty_amount") or Decimal("0.00"))

        if penalty_mode == "FLAT" and flat_penalty_amount <= 0:
            raise serializers.ValidationError(
                {"flat_penalty_amount": "flat_penalty_amount must be greater than 0 when penalty_mode is FLAT."}
            )

        if penalty_mode == "DAILY" and daily_penalty_amount <= 0:
            raise serializers.ValidationError(
                {"daily_penalty_amount": "daily_penalty_amount must be greater than 0 when penalty_mode is DAILY."}
            )

        return attrs

    def create(self, validated_data):
        user = self.context["request"].user
        validated_data["created_by"] = user
        validated_data["payouts_per_period"] = 1
        return super().create(validated_data)


class JoinRequestCreateSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)
    requested_seats = serializers.IntegerField(required=False, min_value=1, max_value=50, default=1)

    def validate(self, attrs):
        request = self.context["request"]
        merry: MerryGoRound = self.context["merry"]

        if MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists():
            raise serializers.ValidationError("You are already a member of this merry.")

        requested_seats = int(attrs.get("requested_seats") or 1)

        if hasattr(merry, "can_accept_join_request"):
            ok, reason = merry.can_accept_join_request(requested_seats)
            if not ok:
                raise serializers.ValidationError(reason)

        return attrs

    def create(self, validated_data):
        request = self.context["request"]
        merry: MerryGoRound = self.context["merry"]

        note = (validated_data.get("note") or "").strip()[:255]
        requested_seats = int(validated_data.get("requested_seats") or 1)

        existing = MerryJoinRequest.objects.filter(merry=merry, user=request.user).first()
        if existing:
            if existing.status == "PENDING":
                changed = False
                if note and note != existing.note:
                    existing.note = note
                    changed = True
                if requested_seats != existing.requested_seats:
                    existing.requested_seats = requested_seats
                    changed = True
                if changed:
                    existing.full_clean()
                    existing.save(update_fields=["note", "requested_seats"])
                return existing

            existing.status = "PENDING"
            existing.note = note
            existing.requested_seats = requested_seats
            existing.reviewed_by = None
            existing.reviewed_at = None
            existing.created_at = timezone.now()
            existing.full_clean()
            existing.save(
                update_fields=[
                    "status",
                    "note",
                    "requested_seats",
                    "reviewed_by",
                    "reviewed_at",
                    "created_at",
                ]
            )
            return existing

        jr = MerryJoinRequest(
            merry=merry,
            user=request.user,
            status="PENDING",
            note=note,
            requested_seats=requested_seats,
        )
        jr.full_clean()
        jr.save()
        return jr


class JoinRequestCancelSerializer(serializers.Serializer):
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
    assigned_seat_numbers = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=False,
    )

    def validate(self, attrs):
        jr: MerryJoinRequest = self.context["join_request"]
        if jr.status != "PENDING":
            raise serializers.ValidationError("Only PENDING requests can be approved.")

        merry = jr.merry
        requested_seats = int(jr.requested_seats or 1)
        if hasattr(merry, "can_accept_join_request"):
            ok, reason = merry.can_accept_join_request(requested_seats)
            if not ok:
                raise serializers.ValidationError(reason)

        assigned = attrs.get("assigned_seat_numbers")
        if assigned is not None and len(assigned) != requested_seats:
            raise serializers.ValidationError(
                {"assigned_seat_numbers": f"Exactly {requested_seats} seat number(s) are required."}
            )

        return attrs

    def save(self, **kwargs):
        jr: MerryJoinRequest = self.context["join_request"]
        admin_user = self.context["request"].user
        assigned = self.validated_data.get("assigned_seat_numbers")
        member, seats = jr.approve(admin_user, assigned_seat_numbers=assigned)
        return {"member": member, "seats": seats}


class AdminRejectJoinRequestSerializer(serializers.Serializer):
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


class EnsureDuesSerializer(serializers.Serializer):
    period_key = serializers.CharField(required=False, allow_blank=True, max_length=50)

    def validate(self, attrs):
        merry: MerryGoRound = self.context["merry"]
        pk = (attrs.get("period_key") or "").strip()
        if not pk:
            pk = current_period_key_for_merry(merry)
        attrs["period_key"] = pk
        return attrs


class CreatePaymentIntentSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    payer_phone = serializers.CharField(required=False, allow_blank=True, max_length=50)

    def validate(self, attrs):
        request = self.context["request"]
        merry: MerryGoRound = self.context["merry"]

        member = MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).first()
        if not member:
            raise serializers.ValidationError("You must be a member of this merry to pay.")

        amt = q2(attrs["amount"])
        if amt <= 0:
            raise serializers.ValidationError({"amount": "Amount must be greater than 0."})
        attrs["amount"] = amt

        payer_phone = (attrs.get("payer_phone") or getattr(request.user, "phone", "") or "").strip()
        if not payer_phone:
            raise serializers.ValidationError({"payer_phone": "payer_phone is required (or user must have a phone)."})
        attrs["payer_phone"] = payer_phone

        attrs["member"] = member
        return attrs


class MerryPaymentBreakdownQuerySerializer(serializers.Serializer):
    include_next = serializers.BooleanField(required=False, default=False)


class CreatePayoutSerializer(serializers.Serializer):
    """
    Compatibility-safe payout creation.

    Queue-based ROSCA uses one current payout event at a time, so slot_no is fixed to 1.
    """
    seat_id = serializers.IntegerField(min_value=1)
    period_key = serializers.CharField(required=False, allow_blank=True, max_length=50)
    slot_no = serializers.IntegerField(required=False, min_value=1)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    compute_amount = serializers.BooleanField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        merry: MerryGoRound = self.context["merry"]

        seat = MerrySeat.objects.filter(id=attrs["seat_id"], merry=merry, is_active=True).first()
        if not seat:
            raise serializers.ValidationError({"seat_id": "Seat not found in this merry."})

        period_key = (attrs.get("period_key") or "").strip()
        if not period_key:
            period_key = current_period_key_for_merry(merry)

        slot_no = attrs.get("slot_no", 1)
        if slot_no != 1:
            raise serializers.ValidationError({"slot_no": "Queue-based ROSCA uses slot_no = 1 only."})

        if MerryPayout.objects.filter(merry=merry, period_key=period_key, slot_no=slot_no).exists():
            raise serializers.ValidationError("A payout already exists for this current payout event.")

        if MerryPayout.objects.filter(merry=merry, seat=seat, period_key=period_key).exists():
            raise serializers.ValidationError("This seat already has a payout in this current payout event.")

        compute_amount = parse_bool(attrs.get("compute_amount"))
        if not compute_amount:
            if attrs.get("amount") is None:
                raise serializers.ValidationError({"amount": "amount is required (or set compute_amount=true)."})
            amt = q2(attrs["amount"])
            if amt <= 0:
                raise serializers.ValidationError({"amount": "Amount must be greater than 0."})
            attrs["amount"] = amt

        attrs["seat"] = seat
        attrs["period_key"] = period_key
        attrs["slot_no"] = 1
        attrs["notes"] = (attrs.get("notes") or "").strip()[:255]
        attrs["compute_amount"] = compute_amount
        return attrs

    def create(self, validated_data):
        merry: MerryGoRound = self.context["merry"]
        seat: MerrySeat = validated_data["seat"]

        amount = validated_data.get("amount")
        if amount is None:
            raise serializers.ValidationError({"amount": "amount is required unless computed in view/service."})

        payout = MerryPayout.objects.create(
            merry=merry,
            seat=seat,
            period_key=validated_data["period_key"],
            slot_no=1,
            amount=amount,
            status="SCHEDULED",
            notes=validated_data["notes"],
        )
        return payout