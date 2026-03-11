# merry/services.py
# FULLY UPDATED — Seat/Shares + Slot-based dues + Payments + Allocations + Seat-based payouts
# + safer parsing/validation + better locking + duplicate receipt protection

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional, List, Tuple

from django.db import IntegrityError, transaction
from django.db.models import Max, Sum
from django.utils import timezone

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
# Domain errors (clean, explicit)
# -----------------------------
class MerryServiceError(Exception):
    """Base domain error for merry services."""


class NotAllowed(MerryServiceError):
    pass


class NotFound(MerryServiceError):
    pass


class BadState(MerryServiceError):
    pass


class Conflict(MerryServiceError):
    pass


# -----------------------------
# Helpers
# -----------------------------
def q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"))


def parse_decimal(value, field_name: str) -> Decimal:
    if value is None or value == "":
        raise BadState(f"{field_name} is required.")
    try:
        return q2(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        raise BadState(f"{field_name} must be a valid number.")


def parse_int(
    value,
    field_name: str,
    *,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> int:
    try:
        n = int(value)
    except (ValueError, TypeError):
        raise BadState(f"{field_name} must be an integer.")

    if min_value is not None and n < min_value:
        raise BadState(f"{field_name} must be >= {min_value}.")
    if max_value is not None and n > max_value:
        raise BadState(f"{field_name} must be <= {max_value}.")
    return n


def parse_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)

    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return default


def is_admin(user) -> bool:
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def get_merry(merry_id: int) -> MerryGoRound:
    merry = MerryGoRound.objects.filter(id=merry_id).select_related("created_by").first()
    if not merry:
        raise NotFound("Merry not found.")
    return merry


def get_join_request(request_id: int) -> MerryJoinRequest:
    jr = MerryJoinRequest.objects.select_related("merry", "user").filter(id=request_id).first()
    if not jr:
        raise NotFound("Join request not found.")
    return jr


def get_active_member(merry: MerryGoRound, user) -> MerryMember:
    member = (
        MerryMember.objects.select_related("merry", "user")
        .filter(merry=merry, user=user, is_active=True)
        .first()
    )
    if not member:
        raise NotFound("You are not an active member of this merry.")
    return member


def get_current_period_key(merry: MerryGoRound) -> str:
    return merry.current_period_key()


def payouts_per_period(merry: MerryGoRound) -> int:
    n = int(getattr(merry, "payouts_per_period", 1) or 1)
    return max(1, n)


def validate_slot(merry: MerryGoRound, slot_no: int) -> None:
    limit = payouts_per_period(merry)
    if slot_no < 1 or slot_no > limit:
        raise BadState(f"slot_no must be between 1 and {limit} for this merry.")


def next_payout_position_for_seat(merry: MerryGoRound) -> int:
    mx = merry.seats.filter(is_active=True).aggregate(m=Max("payout_position")).get("m") or 0
    return int(mx) + 1


def get_next_available_slot(merry: MerryGoRound, period_key: str) -> int:
    limit = payouts_per_period(merry)
    used = set(
        MerryPayout.objects.filter(merry=merry, period_key=period_key).values_list("slot_no", flat=True)
    )
    for s in range(1, limit + 1):
        if s not in used:
            return s
    raise Conflict(f"Payout slots are full for period {period_key}. Max slots: {limit}.")


# ---------- period stepping for carry-forward ----------
def _next_week_period_key(period_key: str) -> str:
    try:
        year = int(period_key[:4])
        week = int(period_key.split("-W")[1])
    except Exception:
        raise BadState("Invalid WEEKLY period_key format. Expected YYYY-W##.")

    from datetime import date, timedelta

    d = date.fromisocalendar(year, week, 1) + timedelta(days=7)
    y, w, _ = d.isocalendar()
    return f"{y:04d}-W{w:02d}"


def _next_month_period_key(period_key: str) -> str:
    try:
        year = int(period_key[:4])
        month = int(period_key.split("-")[1])
    except Exception:
        raise BadState("Invalid MONTHLY period_key format. Expected YYYY-MM.")

    month += 1
    if month == 13:
        month = 1
        year += 1
    return f"{year:04d}-{month:02d}"


def _next_period_key(merry: MerryGoRound, period_key: str) -> str:
    if (merry.payout_frequency or "WEEKLY").upper() == "MONTHLY":
        return _next_month_period_key(period_key)
    return _next_week_period_key(period_key)


# -----------------------------
# Merry lifecycle
# -----------------------------
@transaction.atomic
def create_merry(
    *,
    creator,
    name: str,
    contribution_amount: Decimal,
    cycle_duration_weeks: int = 1,
    payout_order_type: str = "manual",
    next_payout_date=None,
    payout_frequency: str = "WEEKLY",
    payouts_per_period: int = 1,
    is_open: bool = True,
    max_seats: int = 0,
) -> MerryGoRound:
    if not is_admin(creator):
        raise NotAllowed("Admin only.")

    name = (name or "").strip()
    if not name:
        raise BadState("name is required.")

    amount = parse_decimal(contribution_amount, "contribution_amount")
    if amount <= 0:
        raise BadState("contribution_amount must be > 0.")

    cycle_duration_weeks = parse_int(
        cycle_duration_weeks,
        "cycle_duration_weeks",
        min_value=1,
        max_value=520,
    )

    payout_order_type = (payout_order_type or "manual").strip().lower()
    if payout_order_type not in ("manual", "random"):
        raise BadState("payout_order_type must be 'manual' or 'random'.")

    payout_frequency = (payout_frequency or "WEEKLY").upper().strip()
    if payout_frequency not in ("WEEKLY", "MONTHLY"):
        raise BadState("payout_frequency must be 'WEEKLY' or 'MONTHLY'.")

    payouts_per_period = parse_int(
        payouts_per_period,
        "payouts_per_period",
        min_value=1,
        max_value=14,
    )

    is_open = parse_bool(is_open, default=True)
    max_seats = parse_int(max_seats or 0, "max_seats", min_value=0)

    merry = MerryGoRound.objects.create(
        name=name,
        contribution_amount=amount,
        cycle_duration_weeks=cycle_duration_weeks,
        payout_order_type=payout_order_type,
        next_payout_date=next_payout_date or None,
        created_by=creator,
        payout_frequency=payout_frequency,
        payouts_per_period=payouts_per_period,
        is_open=is_open,
        max_seats=max_seats,
    )
    return merry


# -----------------------------
# Slot config (optional)
# -----------------------------
@transaction.atomic
def set_slot_config_bulk(*, admin_user, merry_id: int, items: List[dict]) -> List[MerrySlotConfig]:
    """
    items: [{slot_no: 1, weekday: 0}, ...]
    """
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    merry = get_merry(merry_id)

    if not isinstance(items, list) or not items:
        raise BadState("items must be a non-empty list.")

    seen = set()
    for it in items:
        slot_no = parse_int(it.get("slot_no"), "slot_no", min_value=1)
        weekday = parse_int(it.get("weekday"), "weekday", min_value=0, max_value=6)

        validate_slot(merry, slot_no)

        if slot_no in seen:
            raise BadState("Duplicate slot_no in payload.")
        seen.add(slot_no)

    out: List[MerrySlotConfig] = []
    for it in items:
        slot_no = int(it["slot_no"])
        weekday = int(it["weekday"])
        obj, _ = MerrySlotConfig.objects.get_or_create(
            merry=merry,
            slot_no=slot_no,
            defaults={"weekday": weekday},
        )
        if obj.weekday != weekday:
            obj.weekday = weekday
            obj.full_clean()
            obj.save(update_fields=["weekday"])
        out.append(obj)

    return out


# -----------------------------
# Join requests
# -----------------------------
@transaction.atomic
def request_to_join_merry(
    *,
    user,
    merry_id: int,
    note: str = "",
    requested_seats: int = 1,
) -> MerryJoinRequest:
    merry = MerryGoRound.objects.select_for_update().filter(id=merry_id).first()
    if not merry:
        raise NotFound("Merry not found.")

    if MerryMember.objects.filter(merry=merry, user=user, is_active=True).exists():
        raise Conflict("You are already a member of this merry.")

    note = (note or "").strip()[:255]
    requested_seats = parse_int(
        requested_seats,
        "requested_seats",
        min_value=1,
        max_value=50,
    )

    if hasattr(merry, "can_accept_join_request"):
        ok, reason = merry.can_accept_join_request(requested_seats)
        if not ok:
            raise BadState(reason)

    existing_pending = (
        MerryJoinRequest.objects.select_for_update()
        .filter(merry=merry, user=user, status="PENDING")
        .first()
    )
    if existing_pending:
        changed = False
        if existing_pending.note != note:
            existing_pending.note = note
            changed = True
        if existing_pending.requested_seats != requested_seats:
            existing_pending.requested_seats = requested_seats
            changed = True
        if changed:
            existing_pending.full_clean()
            existing_pending.save(update_fields=["note", "requested_seats"])
        return existing_pending

    existing_latest = (
        MerryJoinRequest.objects.select_for_update()
        .filter(merry=merry, user=user)
        .order_by("-created_at", "-id")
        .first()
    )

    if existing_latest:
        existing_latest.status = "PENDING"
        existing_latest.note = note
        existing_latest.requested_seats = requested_seats
        existing_latest.reviewed_by = None
        existing_latest.reviewed_at = None
        existing_latest.created_at = timezone.now()
        existing_latest.full_clean()
        existing_latest.save(
            update_fields=[
                "status",
                "note",
                "requested_seats",
                "reviewed_by",
                "reviewed_at",
                "created_at",
            ]
        )
        return existing_latest

    jr = MerryJoinRequest(
        merry=merry,
        user=user,
        status="PENDING",
        note=note,
        requested_seats=requested_seats,
    )
    jr.full_clean()
    jr.save()
    return jr


@transaction.atomic
def cancel_join_request(*, user, request_id: int) -> MerryJoinRequest:
    jr = MerryJoinRequest.objects.select_for_update().filter(id=request_id).first()
    if not jr:
        raise NotFound("Join request not found.")
    if jr.user_id != user.id:
        raise NotAllowed("You can only cancel your own join request.")
    if jr.status != "PENDING":
        raise BadState("Only PENDING requests can be cancelled.")

    jr.status = "CANCELLED"
    jr.save(update_fields=["status"])
    return jr


@transaction.atomic
def admin_approve_join_request(*, admin_user, request_id: int) -> Tuple[MerryMember, List[MerrySeat]]:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    jr = (
        MerryJoinRequest.objects.select_for_update()
        .select_related("merry", "user")
        .filter(id=request_id)
        .first()
    )
    if not jr:
        raise NotFound("Join request not found.")
    if jr.status != "PENDING":
        raise BadState("Only PENDING requests can be approved.")

    merry = MerryGoRound.objects.select_for_update().filter(id=jr.merry_id).first()
    if not merry:
        raise NotFound("Merry not found.")

    user = jr.user
    seats_requested = parse_int(jr.requested_seats or 1, "requested_seats", min_value=1, max_value=50)

    if hasattr(merry, "can_accept_join_request"):
        ok, reason = merry.can_accept_join_request(seats_requested)
        if not ok:
            raise BadState(reason)

    member, _ = MerryMember.objects.get_or_create(
        merry=merry,
        user=user,
        defaults={"joined_at": timezone.now(), "is_active": True},
    )

    if not member.is_active:
        member.is_active = True
        if not member.joined_at:
            member.joined_at = timezone.now()
            member.save(update_fields=["is_active", "joined_at"])
        else:
            member.save(update_fields=["is_active"])

    existing_max_seat_no = member.seats.aggregate(m=Max("seat_no")).get("m") or 0
    seat_no_start = int(existing_max_seat_no) + 1

    seats_created: List[MerrySeat] = []
    try:
        for i in range(seats_requested):
            payout_position: Optional[int] = None
            if merry.payout_order_type == "manual":
                payout_position = next_payout_position_for_seat(merry)

            seat = MerrySeat.objects.create(
                merry=merry,
                member=member,
                seat_no=seat_no_start + i,
                payout_position=payout_position,
                is_active=True,
                created_at=timezone.now(),
            )
            seats_created.append(seat)
    except IntegrityError:
        raise Conflict("Failed to create seats (duplicate payout_position). Try again.")

    jr.status = "APPROVED"
    jr.reviewed_by = admin_user
    jr.reviewed_at = timezone.now()
    jr.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    return member, seats_created


@transaction.atomic
def admin_reject_join_request(*, admin_user, request_id: int, note: str = "") -> MerryJoinRequest:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    jr = MerryJoinRequest.objects.select_for_update().filter(id=request_id).first()
    if not jr:
        raise NotFound("Join request not found.")
    if jr.status != "PENDING":
        raise BadState("Only PENDING requests can be rejected.")

    jr.status = "REJECTED"
    jr.reviewed_by = admin_user
    jr.reviewed_at = timezone.now()
    if note:
        jr.note = (note or "").strip()[:255]
    jr.save(update_fields=["status", "reviewed_by", "reviewed_at", "note"])
    return jr


# -----------------------------
# Dues scheduling
# -----------------------------
@transaction.atomic
def ensure_dues_for_period(*, admin_user, merry_id: int, period_key: Optional[str] = None) -> int:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")
    merry = get_merry(merry_id)
    pk = (period_key or "").strip() or get_current_period_key(merry)
    return merry.ensure_dues_for_period(period_key=pk)


@transaction.atomic
def ensure_dues_for_member_period(merry: MerryGoRound, member: MerryMember, period_key: str) -> None:
    """
    Ensures dues exist for member's ACTIVE seats for a given period (all slots).
    Used by allocation to avoid missing rows.
    """
    due_amt = merry.contribution_amount or Decimal("0")
    slots = payouts_per_period(merry)

    active_seats = list(
        MerrySeat.objects.select_for_update()
        .filter(merry=merry, member=member, is_active=True)
        .order_by("seat_no", "id")
    )

    for seat in active_seats:
        for slot_no in range(1, slots + 1):
            MerryContributionDue.objects.get_or_create(
                merry=merry,
                seat=seat,
                period_key=period_key,
                slot_no=slot_no,
                defaults={
                    "due_amount": due_amt,
                    "paid_amount": Decimal("0"),
                    "status": "PENDING",
                    "due_date": None,
                },
            )


# -----------------------------
# Payments (intent + confirm + allocate)
# -----------------------------
@transaction.atomic
def create_payment_intent(*, user, merry_id: int, amount: Decimal, payer_phone: str) -> MerryPayment:
    merry = get_merry(merry_id)
    member = get_active_member(merry, user)

    amt = parse_decimal(amount, "amount")
    if amt <= 0:
        raise BadState("amount must be > 0.")

    payer_phone = (payer_phone or "").strip()
    if not payer_phone:
        raise BadState("payer_phone is required.")

    period_key = get_current_period_key(merry)
    ensure_dues_for_member_period(merry, member, period_key)

    return MerryPayment.objects.create(
        merry=merry,
        beneficiary_member=member,
        initiated_by=user,
        payer_phone=payer_phone,
        period_key=period_key,
        amount=amt,
        status="PENDING",
    )


@transaction.atomic
def confirm_payment_and_allocate(
    *,
    payment_id: int,
    mpesa_receipt_number: Optional[str] = None,
    paid_at=None,
) -> MerryPayment:
    p = (
        MerryPayment.objects.select_for_update()
        .select_related("merry", "beneficiary_member", "beneficiary_member__user")
        .filter(id=payment_id)
        .first()
    )
    if not p:
        raise NotFound("Payment not found.")

    if mpesa_receipt_number:
        receipt = (mpesa_receipt_number or "").strip()[:64]
        exists_elsewhere = (
            MerryPayment.objects.exclude(id=p.id)
            .filter(mpesa_receipt_number=receipt)
            .exists()
        )
        if exists_elsewhere:
            raise Conflict("This M-Pesa receipt number is already used.")
    else:
        receipt = None

    if p.status == "CONFIRMED":
        return p

    if p.status in ("CANCELLED",):
        raise BadState(f"Cannot confirm payment from status={p.status}")

    p.status = "CONFIRMED"
    p.paid_at = paid_at or timezone.now()
    if receipt:
        p.mpesa_receipt_number = receipt

    if receipt:
        p.save(update_fields=["status", "paid_at", "mpesa_receipt_number"])
    else:
        p.save(update_fields=["status", "paid_at"])

    allocate_payment(payment_id=p.id)
    return p


@transaction.atomic
def mark_payment_failed(*, payment_id: int) -> MerryPayment:
    p = MerryPayment.objects.select_for_update().filter(id=payment_id).first()
    if not p:
        raise NotFound("Payment not found.")
    if p.status == "CONFIRMED":
        raise BadState("Cannot fail a CONFIRMED payment.")
    if p.status == "CANCELLED":
        raise BadState("Cannot fail a CANCELLED payment.")

    p.status = "FAILED"
    p.save(update_fields=["status"])
    return p


@transaction.atomic
def allocate_payment(*, payment_id: int) -> MerryPayment:
    """
    Allocates CONFIRMED payment into dues:
      period -> slot 1..N -> seat_no 1..N
    Supports partial payments + overpayment carry-forward to next periods.
    """
    payment = (
        MerryPayment.objects.select_for_update()
        .select_related("merry", "beneficiary_member", "beneficiary_member__user")
        .get(id=payment_id)
    )

    if payment.status != "CONFIRMED":
        raise BadState("Payment must be CONFIRMED before allocation.")

    merry = payment.merry
    member = payment.beneficiary_member

    if member.merry_id != merry.id:
        raise BadState("Payment beneficiary does not belong to this merry.")

    if not member.is_active:
        raise BadState("Cannot allocate payment for an inactive member.")

    remaining = payment.amount or Decimal("0")
    if remaining <= 0:
        raise BadState("Payment amount must be > 0.")

    period_key = (payment.period_key or "").strip() or get_current_period_key(merry)

    safety = 0
    while remaining > 0:
        safety += 1
        if safety > 2000:
            raise BadState("Allocation safety limit reached.")

        ensure_dues_for_member_period(merry, member, period_key)

        dues = list(
            MerryContributionDue.objects.select_for_update()
            .filter(
                merry=merry,
                seat__member=member,
                seat__is_active=True,
                period_key=period_key,
                status__in=["PENDING", "PARTIAL"],
            )
            .select_related("seat")
            .order_by("slot_no", "seat__seat_no", "id")
        )

        any_needed = False
        for due in dues:
            need = (due.due_amount or Decimal("0")) - (due.paid_amount or Decimal("0"))
            if need <= 0:
                continue

            any_needed = True
            alloc = remaining if remaining < need else need
            if alloc <= 0:
                continue

            allocation, _ = MerryPaymentAllocation.objects.get_or_create(
                payment=payment,
                due=due,
                defaults={"amount_allocated": Decimal("0")},
            )
            allocation.amount_allocated = (allocation.amount_allocated or Decimal("0")) + alloc
            allocation.full_clean()
            allocation.save(update_fields=["amount_allocated"])

            due.paid_amount = (due.paid_amount or Decimal("0")) + alloc
            due.recalc_status()
            due.save(update_fields=["paid_amount", "status", "updated_at"])

            remaining -= alloc
            if remaining <= 0:
                break

        if remaining <= 0:
            break

        period_key = _next_period_key(merry, period_key)

        if not any_needed:
            continue

    return payment


# -----------------------------
# Payouts (seat-based)
# -----------------------------
@transaction.atomic
def compute_payout_amount_for_slot(*, merry_id: int, period_key: str, slot_no: int) -> Decimal:
    merry = get_merry(merry_id)
    validate_slot(merry, slot_no)

    pk = (period_key or "").strip()
    if not pk:
        raise BadState("period_key is required.")

    total = (
        MerryContributionDue.objects.filter(merry=merry, period_key=pk, slot_no=slot_no)
        .aggregate(s=Sum("paid_amount"))
        .get("s")
        or Decimal("0")
    )
    return q2(total)


@transaction.atomic
def create_payout_record(
    *,
    admin_user,
    merry_id: int,
    seat_id: int,
    amount: Decimal,
    period_key: Optional[str] = None,
    slot_no: Optional[int] = None,
    notes: str = "",
) -> MerryPayout:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    merry = MerryGoRound.objects.select_for_update().filter(id=merry_id).first()
    if not merry:
        raise NotFound("Merry not found.")

    seat = (
        MerrySeat.objects.select_for_update()
        .filter(id=seat_id, merry=merry, is_active=True)
        .select_related("member", "member__user")
        .first()
    )
    if not seat:
        raise NotFound("Seat not found in this merry.")

    amt = parse_decimal(amount, "amount")
    if amt <= 0:
        raise BadState("amount must be > 0.")

    pk = (period_key or "").strip() or get_current_period_key(merry)

    if slot_no is None:
        slot_no = get_next_available_slot(merry, pk)
    else:
        slot_no = parse_int(slot_no, "slot_no", min_value=1)
        validate_slot(merry, slot_no)
        if MerryPayout.objects.filter(merry=merry, period_key=pk, slot_no=slot_no).exists():
            raise Conflict(f"Slot {slot_no} is already used for period {pk}.")

    if MerryPayout.objects.filter(merry=merry, seat=seat, period_key=pk).exists():
        raise Conflict("This seat already has a payout record in this period.")

    try:
        payout = MerryPayout.objects.create(
            merry=merry,
            seat=seat,
            period_key=pk,
            slot_no=slot_no,
            amount=amt,
            status="SCHEDULED",
            notes=(notes or "").strip()[:255],
        )
    except IntegrityError:
        raise Conflict("Failed to create payout (duplicate slot or seat payout). Try again.")

    return payout


@transaction.atomic
def mark_payout_paid(*, payout_id: int, paid_at=None) -> MerryPayout:
    p = MerryPayout.objects.select_for_update().filter(id=payout_id).first()
    if not p:
        raise NotFound("Payout not found.")

    if p.status == "PAID":
        return p

    if p.status == "CANCELLED":
        raise BadState(f"Cannot mark PAID from status={p.status}")

    p.status = "PAID"
    p.paid_at = paid_at or timezone.now()
    p.save(update_fields=["status", "paid_at"])
    return p


# -----------------------------
# Read helpers (optional)
# -----------------------------
def list_my_payments(*, user, limit: int = 200):
    limit = parse_int(limit, "limit", min_value=1, max_value=1000)
    return (
        MerryPayment.objects.filter(beneficiary_member__user=user)
        .select_related("merry", "beneficiary_member", "beneficiary_member__user")
        .order_by("-created_at")[:limit]
    )


def list_dues_for_member(*, user, merry_id: int, period_key: Optional[str] = None):
    merry = get_merry(merry_id)
    member = get_active_member(merry, user)
    pk = (period_key or "").strip() or get_current_period_key(merry)

    with transaction.atomic():
        ensure_dues_for_member_period(merry, member, pk)

    return (
        MerryContributionDue.objects.filter(
            merry=merry,
            seat__member=member,
            seat__is_active=True,
            period_key=pk,
        )
        .select_related("seat")
        .order_by("slot_no", "seat__seat_no", "id")
    )