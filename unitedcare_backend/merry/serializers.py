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
)


# -----------------------------
# Small utilities
# -----------------------------
def q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


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
      - WEEKLY  => "YYYY-Www"
      - MONTHLY => "YYYY-MM"
    """
    today = timezone.now().date()
    freq = (getattr(merry, "payout_frequency", None) or "WEEKLY").upper()
    if freq == "MONTHLY":
        return f"{today.year}-{today.month:02d}"
    iso_year, iso_week, _ = today.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def payouts_per_period(merry: MerryGoRound) -> int:
    n = int(getattr(merry, "payouts_per_period", 1) or 1)
    return max(1, n)


# -----------------------------
# READ serializers (responses)
# -----------------------------
class MerryGoRoundSerializer(serializers.ModelSerializer):
    members_count = serializers.SerializerMethodField()
    seats_count = serializers.SerializerMethodField()
    total_pool_per_slot = serializers.SerializerMethodField()
    total_pool_per_period = serializers.SerializerMethodField()

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
        # requires method on model; if not present, compute as seats_count * contribution_amount
        if hasattr(obj, "total_pool_per_slot"):
            return str(obj.total_pool_per_slot())
        seats = obj.seats.filter(is_active=True).count()
        return str(Decimal(seats) * (obj.contribution_amount or Decimal("0")))

    def get_total_pool_per_period(self, obj: MerryGoRound) -> str:
        if hasattr(obj, "total_pool_per_period"):
            return str(obj.total_pool_per_period())
        seats = obj.seats.filter(is_active=True).count()
        return str(Decimal(seats) * (obj.contribution_amount or Decimal("0")) * Decimal(obj.payouts_per_period or 1))


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
        ]
        read_only_fields = fields


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
            "requested_seats",
            "reviewed_by",
            "reviewed_at",
            "created_at",
        ]
        read_only_fields = ["id", "status", "reviewed_by", "reviewed_at", "created_at", "user"]


class MerrySlotConfigSerializer(serializers.ModelSerializer):
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
            "period_key",
            "slot_no",
            "due_amount",
            "paid_amount",
            "outstanding",
            "status",
            "due_date",
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
    payment_amount = serializers.DecimalField(source="payment.amount", read_only=True)
    due_period_key = serializers.CharField(source="due.period_key", read_only=True)
    due_slot_no = serializers.IntegerField(source="due.slot_no", read_only=True)
    due_seat_no = serializers.IntegerField(source="due.seat.seat_no", read_only=True)

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
            "amount_allocated",
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
            "period_key",
            "slot_no",
            "amount",
            "status",
            "paid_at",
            "notes",
            "created_at",
        ]
        read_only_fields = ["id", "status", "paid_at", "created_at", "merry_name", "seat_no", "member_id", "user_id", "username", "phone"]


# -----------------------------
# WRITE serializers (inputs)
# -----------------------------
class CreateMerrySerializer(serializers.ModelSerializer):
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
        if v not in ("WEEKLY", "MONTHLY"):
            raise serializers.ValidationError("payout_frequency must be 'WEEKLY' or 'MONTHLY'.")
        return v

    def validate_payouts_per_period(self, value: int):
        if value < 1 or value > 14:
            raise serializers.ValidationError("payouts_per_period must be between 1 and 14.")
        return value

    def create(self, validated_data):
        user = self.context["request"].user
        validated_data["created_by"] = user
        return super().create(validated_data)


class JoinRequestCreateSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)
    requested_seats = serializers.IntegerField(required=False, min_value=1, max_value=50, default=1)

    def validate(self, attrs):
        request = self.context["request"]
        merry: MerryGoRound = self.context["merry"]

        if MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists():
            raise serializers.ValidationError("You are already a member of this merry.")
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
            existing.save(update_fields=["status", "note", "requested_seats", "reviewed_by", "reviewed_at", "created_at"])
            return existing

        jr = MerryJoinRequest(merry=merry, user=request.user, status="PENDING", note=note, requested_seats=requested_seats)
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
    def validate(self, attrs):
        jr: MerryJoinRequest = self.context["join_request"]
        if jr.status != "PENDING":
            raise serializers.ValidationError("Only PENDING requests can be approved.")
        return attrs

    def save(self, **kwargs):
        jr: MerryJoinRequest = self.context["join_request"]
        admin_user = self.context["request"].user
        # jr.approve now returns (member, seats)
        member, seats = jr.approve(admin_user)
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
    period_key = serializers.CharField(required=False, allow_blank=True, max_length=20)

    def validate(self, attrs):
        merry: MerryGoRound = self.context["merry"]
        pk = (attrs.get("period_key") or "").strip()
        if not pk:
            pk = current_period_key_for_merry(merry)
        attrs["period_key"] = pk
        return attrs


class CreatePaymentIntentSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    payer_phone = serializers.CharField(required=False, allow_blank=True, max_length=20)

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


class CreatePayoutSerializer(serializers.Serializer):
    """
    Seat-based payout creation
    """
    seat_id = serializers.IntegerField(min_value=1)
    period_key = serializers.CharField(required=False, allow_blank=True, max_length=20)
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

        limit = payouts_per_period(merry)

        slot_no = attrs.get("slot_no")
        if slot_no is None:
            used = set(
                MerryPayout.objects.filter(merry=merry, period_key=period_key)
                .values_list("slot_no", flat=True)
            )
            chosen = None
            for s in range(1, limit + 1):
                if s not in used:
                    chosen = s
                    break
            if chosen is None:
                raise serializers.ValidationError({"slot_no": f"Payout slots full for {period_key}. Max {limit}."})
            slot_no = chosen
        else:
            if slot_no < 1 or slot_no > limit:
                raise serializers.ValidationError({"slot_no": f"slot_no must be between 1 and {limit}."})

        if MerryPayout.objects.filter(merry=merry, period_key=period_key, slot_no=slot_no).exists():
            raise serializers.ValidationError("A payout already exists for this period slot.")

        if MerryPayout.objects.filter(merry=merry, seat=seat, period_key=period_key).exists():
            raise serializers.ValidationError("This seat already has a payout in this period.")

        compute_amount = parse_bool(attrs.get("compute_amount"))
        if compute_amount:
            # service/view will compute
            pass
        else:
            if attrs.get("amount") is None:
                raise serializers.ValidationError({"amount": "amount is required (or set compute_amount=true)."})
            amt = q2(attrs["amount"])
            if amt <= 0:
                raise serializers.ValidationError({"amount": "Amount must be greater than 0."})
            attrs["amount"] = amt

        attrs["seat"] = seat
        attrs["period_key"] = period_key
        attrs["slot_no"] = slot_no
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
            slot_no=validated_data["slot_no"],
            amount=amount,
            status="SCHEDULED",
            notes=validated_data["notes"],
        )
        return payout