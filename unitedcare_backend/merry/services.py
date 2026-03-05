# merry/services.py
# UPDATED — Seat/Shares + Slot-based dues + Payments + Allocations + Seat-based payouts

from __future__ import annotations

from decimal import Decimal
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


def is_admin(user) -> bool:
    # align with your views.py
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def get_merry(merry_id: int) -> MerryGoRound:
    merry = MerryGoRound.objects.filter(id=merry_id).first()
    if not merry:
        raise NotFound("Merry not found.")
    return merry


def get_join_request(request_id: int) -> MerryJoinRequest:
    jr = MerryJoinRequest.objects.select_related("merry", "user").filter(id=request_id).first()
    if not jr:
        raise NotFound("Join request not found.")
    return jr


def get_active_member(merry: MerryGoRound, user) -> MerryMember:
    m = MerryMember.objects.select_related("merry", "user").filter(
        merry=merry, user=user, is_active=True
    ).first()
    if not m:
        raise NotFound("You are not an active member of this merry.")
    return m


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
        MerryPayout.objects.filter(merry=merry, period_key=period_key)
        .values_list("slot_no", flat=True)
    )
    for s in range(1, limit + 1):
        if s not in used:
            return s
    raise Conflict(f"Payout slots are full for period {period_key}. Max slots: {limit}.")


# ---------- period stepping for carry-forward ----------
def _next_week_period_key(period_key: str) -> str:
    # format: YYYY-W##
    try:
        year = int(period_key[:4])
        week = int(period_key.split("-W")[1])
    except Exception:
        raise BadState("Invalid WEEKLY period_key format. Expected YYYY-W##")

    from datetime import date, timedelta

    d = date.fromisocalendar(year, week, 1) + timedelta(days=7)
    y, w, _ = d.isocalendar()
    return f"{y:04d}-W{w:02d}"


def _next_month_period_key(period_key: str) -> str:
    # format: YYYY-MM
    try:
        year = int(period_key[:4])
        month = int(period_key.split("-")[1])
    except Exception:
        raise BadState("Invalid MONTHLY period_key format. Expected YYYY-MM")

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
) -> MerryGoRound:
    if not is_admin(creator):
        raise NotAllowed("Admin only.")

    name = (name or "").strip()
    if not name:
        raise BadState("name is required.")

    amount = q2(Decimal(str(contribution_amount)))
    if amount <= 0:
        raise BadState("contribution_amount must be > 0.")

    try:
        cycle_duration_weeks = int(cycle_duration_weeks)
    except Exception:
        raise BadState("cycle_duration_weeks must be an integer.")
    if cycle_duration_weeks < 1 or cycle_duration_weeks > 520:
        raise BadState("cycle_duration_weeks must be between 1 and 520.")

    if payout_order_type not in ("manual", "random"):
        raise BadState("payout_order_type must be 'manual' or 'random'.")

    payout_frequency = (payout_frequency or "WEEKLY").upper()
    if payout_frequency not in ("WEEKLY", "MONTHLY"):
        raise BadState("payout_frequency must be 'WEEKLY' or 'MONTHLY'.")

    try:
        payouts_per_period = int(payouts_per_period)
    except Exception:
        raise BadState("payouts_per_period must be an integer.")
    if payouts_per_period < 1 or payouts_per_period > 14:
        raise BadState("payouts_per_period must be between 1 and 14.")

    merry = MerryGoRound.objects.create(
        name=name,
        contribution_amount=amount,
        cycle_duration_weeks=cycle_duration_weeks,
        payout_order_type=payout_order_type,
        next_payout_date=next_payout_date or None,
        created_by=creator,
        payout_frequency=payout_frequency,
        payouts_per_period=payouts_per_period,
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
        try:
            slot_no = int(it.get("slot_no"))
            weekday = int(it.get("weekday"))
        except Exception:
            raise BadState("slot_no and weekday must be integers.")
        validate_slot(merry, slot_no)
        if weekday < 0 or weekday > 6:
            raise BadState("weekday must be 0..6 (Mon..Sun).")
        if slot_no in seen:
            raise BadState("Duplicate slot_no in payload.")
        seen.add(slot_no)

    out: List[MerrySlotConfig] = []
    for it in items:
        slot_no = int(it["slot_no"])
        weekday = int(it["weekday"])
        obj, _ = MerrySlotConfig.objects.get_or_create(
            merry=merry, slot_no=slot_no, defaults={"weekday": weekday}
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
    merry = get_merry(merry_id)

    if MerryMember.objects.filter(merry=merry, user=user, is_active=True).exists():
        raise Conflict("You are already a member of this merry.")

    note = (note or "").strip()[:255]

    try:
        requested_seats = int(requested_seats)
    except Exception:
        raise BadState("requested_seats must be an integer.")
    if requested_seats < 1 or requested_seats > 50:
        raise BadState("requested_seats must be between 1 and 50.")

    existing = MerryJoinRequest.objects.select_for_update().filter(merry=merry, user=user).first()

    if existing:
        if existing.status == "PENDING":
            # update fields if needed
            changed = False
            if existing.note != note:
                existing.note = note
                changed = True
            if existing.requested_seats != requested_seats:
                existing.requested_seats = requested_seats
                changed = True
            if changed:
                existing.full_clean()
                existing.save(update_fields=["note", "requested_seats"])
            return existing

        # resubmit
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
        merry=merry, user=user, status="PENDING", note=note, requested_seats=requested_seats
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

    merry = jr.merry
    user = jr.user
    seats_requested = int(jr.requested_seats or 1)

    member, _ = MerryMember.objects.get_or_create(
        merry=merry,
        user=user,
        defaults={"joined_at": timezone.now(), "is_active": True},
    )
    if not member.is_active:
        member.is_active = True
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

    amt = q2(Decimal(str(amount)))
    if amt <= 0:
        raise BadState("amount must be > 0.")

    payer_phone = (payer_phone or "").strip()
    if not payer_phone:
        raise BadState("payer_phone is required.")

    period_key = get_current_period_key(merry)

    # ensure at least this member has dues in current period
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

    if p.status == "CONFIRMED":
        return p

    if p.status not in ("PENDING", "FAILED"):
        raise BadState(f"Cannot confirm payment from status={p.status}")

    if mpesa_receipt_number:
        p.mpesa_receipt_number = (mpesa_receipt_number or "").strip()[:64]

    p.status = "CONFIRMED"
    p.paid_at = paid_at or timezone.now()
    p.save(update_fields=["status", "paid_at", "mpesa_receipt_number"])

    allocate_payment(payment_id=p.id)
    return p


@transaction.atomic
def mark_payment_failed(*, payment_id: int) -> MerryPayment:
    p = MerryPayment.objects.select_for_update().filter(id=payment_id).first()
    if not p:
        raise NotFound("Payment not found.")
    if p.status == "CONFIRMED":
        raise BadState("Cannot fail a CONFIRMED payment.")
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
        .select_related("merry", "beneficiary_member")
        .get(id=payment_id)
    )

    if payment.status != "CONFIRMED":
        raise BadState("Payment must be CONFIRMED before allocation.")

    merry = payment.merry
    member = payment.beneficiary_member

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

            a, _ = MerryPaymentAllocation.objects.get_or_create(
                payment=payment,
                due=due,
                defaults={"amount_allocated": Decimal("0")},
            )
            a.amount_allocated = (a.amount_allocated or Decimal("0")) + alloc
            a.full_clean()
            a.save(update_fields=["amount_allocated"])

            due.paid_amount = (due.paid_amount or Decimal("0")) + alloc
            due.recalc_status()
            due.save(update_fields=["paid_amount", "status", "updated_at"])

            remaining -= alloc
            if remaining <= 0:
                break

        if remaining <= 0:
            break

        # if nothing outstanding in this period, jump forward
        if not any_needed:
            period_key = _next_period_key(merry, period_key)
            continue

        # if fully satisfied and still remaining, go next period
        period_key = _next_period_key(merry, period_key)

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

    merry = get_merry(merry_id)

    seat = MerrySeat.objects.filter(id=seat_id, merry=merry, is_active=True).select_related("member").first()
    if not seat:
        raise NotFound("Seat not found in this merry.")

    amt = q2(Decimal(str(amount)))
    if amt <= 0:
        raise BadState("amount must be > 0.")

    pk = (period_key or "").strip() or get_current_period_key(merry)

    if slot_no is None:
        slot_no = get_next_available_slot(merry, pk)
    else:
        try:
            slot_no = int(slot_no)
        except Exception:
            raise BadState("slot_no must be an integer.")
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
    if p.status not in ("SCHEDULED", "PROCESSING", "FAILED"):
        raise BadState(f"Cannot mark PAID from status={p.status}")
    p.status = "PAID"
    p.paid_at = paid_at or timezone.now()
    p.save(update_fields=["status", "paid_at"])
    return p


# -----------------------------
# Read helpers (optional)
# -----------------------------
def list_my_payments(*, user, limit: int = 200):
    return (
        MerryPayment.objects.filter(beneficiary_member__user=user)
        .select_related("merry", "beneficiary_member", "beneficiary_member__user")
        .order_by("-created_at")[:limit]
    )


def list_dues_for_member(*, user, merry_id: int, period_key: Optional[str] = None):
    merry = get_merry(merry_id)
    member = get_active_member(merry, user)
    pk = (period_key or "").strip() or get_current_period_key(merry)

    # safe auto-ensure (member only)
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