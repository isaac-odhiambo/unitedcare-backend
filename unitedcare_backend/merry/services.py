# merry/services.py
"""
Production-ready business logic for Merry (merry-go-round) app.

Goals:
- Keep views thin
- Centralize business rules
- Use atomic transactions where money/state changes happen
- Provide clear errors for API layer to translate into DRF responses

Assumptions:
- Admin check is done in views/permissions, but we still guard in services.
- Payments integration (STK/B2C, ledger posting, MpesaTransaction linking) is handled
  in the Payments app. This service prepares/validates Merry records and can be
  called by Payments callbacks to mark records paid.

Models used:
- MerryGoRound, MerryMember, MerryJoinRequest, MerryContribution, MerryPayout
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from .models import (
    MerryGoRound,
    MerryMember,
    MerryJoinRequest,
    MerryContribution,
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
    # Adjust to your role system (e.g. user.role == "admin")
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


def get_member(member_id: int) -> MerryMember:
    m = MerryMember.objects.select_related("merry", "user").filter(id=member_id).first()
    if not m:
        raise NotFound("Member not found.")
    return m


def get_active_member(merry: MerryGoRound, user) -> MerryMember:
    m = MerryMember.objects.select_related("merry").filter(merry=merry, user=user, is_active=True).first()
    if not m:
        raise NotFound("You are not an active member of this merry.")
    return m


def current_week_number(merry: MerryGoRound) -> int:
    """
    Simple and predictable week_number:
      - Week 1 begins at merry.created_at date.
      - Increments every 7 days.
    """
    start = merry.created_at.date()
    today = timezone.now().date()
    delta_days = (today - start).days
    if delta_days < 0:
        delta_days = 0
    return (delta_days // 7) + 1


def next_payout_position(merry: MerryGoRound) -> int:
    """
    Next manual payout position = max(payout_position)+1 among active members.
    """
    mx = merry.members.filter(is_active=True).aggregate(m=Max("payout_position")).get("m") or 0
    return int(mx) + 1


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
) -> MerryGoRound:
    if not is_admin(creator):
        raise NotAllowed("Admin only.")

    name = (name or "").strip()
    if not name:
        raise BadState("name is required.")

    amount = q2(Decimal(str(contribution_amount)))
    if amount <= 0:
        raise BadState("contribution_amount must be > 0.")

    if cycle_duration_weeks < 1 or cycle_duration_weeks > 52:
        raise BadState("cycle_duration_weeks must be between 1 and 52.")

    if payout_order_type not in ("manual", "random"):
        raise BadState("payout_order_type must be 'manual' or 'random'.")

    merry = MerryGoRound.objects.create(
        name=name,
        contribution_amount=amount,
        cycle_duration_weeks=cycle_duration_weeks,
        payout_order_type=payout_order_type,
        next_payout_date=next_payout_date or None,
        created_by=creator,
    )
    return merry


# -----------------------------
# Join requests (member -> admin approval)
# -----------------------------
@transaction.atomic
def request_to_join_merry(*, user, merry_id: int, note: str = "") -> MerryJoinRequest:
    merry = get_merry(merry_id)

    if MerryMember.objects.filter(merry=merry, user=user, is_active=True).exists():
        raise Conflict("You are already a member of this merry.")

    note = (note or "").strip()[:255]

    existing = MerryJoinRequest.objects.select_for_update().filter(merry=merry, user=user).first()

    if existing:
        if existing.status == "PENDING":
            # update note if provided
            if note and note != existing.note:
                existing.note = note
                existing.save(update_fields=["note"])
            return existing

        # allow resubmit after reject/cancel/approved (approved won't happen if member exists, but keep consistent)
        existing.status = "PENDING"
        existing.note = note
        existing.reviewed_by = None
        existing.reviewed_at = None
        existing.created_at = timezone.now()
        existing.full_clean()
        existing.save(update_fields=["status", "note", "reviewed_by", "reviewed_at", "created_at"])
        return existing

    jr = MerryJoinRequest(merry=merry, user=user, status="PENDING", note=note)
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
def admin_approve_join_request(*, admin_user, request_id: int) -> MerryMember:
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    jr = MerryJoinRequest.objects.select_for_update().select_related("merry", "user").filter(id=request_id).first()
    if not jr:
        raise NotFound("Join request not found.")
    if jr.status != "PENDING":
        raise BadState("Only PENDING requests can be approved.")

    merry = jr.merry
    user = jr.user

    # If already member (rare race), just mark approved
    existing_member = MerryMember.objects.filter(merry=merry, user=user, is_active=True).first()
    if existing_member:
        jr.status = "APPROVED"
        jr.reviewed_by = admin_user
        jr.reviewed_at = timezone.now()
        jr.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        return existing_member

    payout_position: Optional[int] = None
    if merry.payout_order_type == "manual":
        payout_position = next_payout_position(merry)

    try:
        member = MerryMember.objects.create(
            merry=merry,
            user=user,
            payout_position=payout_position,
            joined_at=timezone.now(),
            is_active=True,
        )
    except IntegrityError:
        # handle unique constraints (e.g. payout_position race)
        raise Conflict("Failed to create member (possible duplicate payout position). Try again.")

    jr.status = "APPROVED"
    jr.reviewed_by = admin_user
    jr.reviewed_at = timezone.now()
    jr.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    return member


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
# Contributions
# -----------------------------
@transaction.atomic
def create_contribution_intent(
    *,
    user,
    merry_id: int,
    week_number: Optional[int] = None,
) -> MerryContribution:
    """
    Creates MerryContribution(PENDING) for the user as an active member.

    Payments app should then:
      - initiate STK push
      - link MpesaTransaction.target_object -> this contribution (GenericForeignKey)
      - on callback success: mark contribution paid + ledger entries
    """
    merry = get_merry(merry_id)
    member = get_active_member(merry, user)

    if week_number is None:
        week_number = current_week_number(merry)

    try:
        week_number = int(week_number)
    except Exception:
        raise BadState("week_number must be an integer.")
    if week_number <= 0:
        raise BadState("week_number must be >= 1.")

    existing = MerryContribution.objects.filter(member=member, week_number=week_number).first()
    if existing:
        raise Conflict("Contribution already exists for this week.")

    contribution = MerryContribution.objects.create(
        member=member,
        week_number=week_number,
        amount=merry.contribution_amount,
        status="PENDING",
    )
    return contribution


@transaction.atomic
def mark_contribution_paid(
    *,
    contribution_id: int,
    mpesa_receipt_number: Optional[str] = None,
    paid_at=None,
) -> MerryContribution:
    """
    Called by Payments callback on successful STK.
    Keeps state change atomic.
    """
    c = MerryContribution.objects.select_for_update().filter(id=contribution_id).first()
    if not c:
        raise NotFound("Contribution not found.")

    if c.status == "PAID":
        return c

    if c.status not in ("PENDING", "FAILED"):
        raise BadState(f"Cannot mark contribution paid from status={c.status}")

    c.status = "PAID"
    c.paid_at = paid_at or timezone.now()
    if mpesa_receipt_number:
        c.mpesa_receipt_number = (mpesa_receipt_number or "").strip()[:64]

    c.save(update_fields=["status", "paid_at", "mpesa_receipt_number"])
    return c


@transaction.atomic
def mark_contribution_failed(*, contribution_id: int) -> MerryContribution:
    c = MerryContribution.objects.select_for_update().filter(id=contribution_id).first()
    if not c:
        raise NotFound("Contribution not found.")
    if c.status == "PAID":
        raise BadState("Cannot fail a PAID contribution.")
    c.status = "FAILED"
    c.save(update_fields=["status"])
    return c


# -----------------------------
# Payout records (payments app does actual money transfer)
# -----------------------------
@transaction.atomic
def create_payout_record(
    *,
    admin_user,
    merry_id: int,
    member_id: int,
    amount: Decimal,
    week_number: Optional[int] = None,
    notes: str = "",
) -> MerryPayout:
    """
    Admin creates a payout record (SCHEDULED).
    Actual withdrawal request + B2C payout happens in Payments app.
    """
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")

    merry = get_merry(merry_id)

    member = MerryMember.objects.filter(id=member_id, merry=merry, is_active=True).first()
    if not member:
        raise NotFound("Member not found in this merry.")

    amount = q2(Decimal(str(amount)))
    if amount <= 0:
        raise BadState("amount must be > 0.")

    if week_number is None:
        week_number = current_week_number(merry)

    try:
        week_number = int(week_number)
    except Exception:
        raise BadState("week_number must be an integer.")
    if week_number <= 0:
        raise BadState("week_number must be >= 1.")

    # one payout per merry/week
    if MerryPayout.objects.filter(merry=merry, week_number=week_number).exists():
        raise Conflict("A payout already exists for this merry and week.")

    payout = MerryPayout.objects.create(
        merry=merry,
        member=member,
        week_number=week_number,
        amount=amount,
        status="SCHEDULED",
        notes=(notes or "").strip()[:255],
    )
    return payout


@transaction.atomic
def mark_payout_processing(*, payout_id: int) -> MerryPayout:
    """
    Called when Payments withdrawal is initiated.
    """
    p = MerryPayout.objects.select_for_update().filter(id=payout_id).first()
    if not p:
        raise NotFound("Payout not found.")
    if p.status in ("PAID", "CANCELLED"):
        raise BadState(f"Cannot set PROCESSING from status={p.status}")
    p.status = "PROCESSING"
    p.save(update_fields=["status"])
    return p


@transaction.atomic
def mark_payout_paid(*, payout_id: int, paid_at=None) -> MerryPayout:
    """
    Called by Payments callback on B2C success.
    """
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


@transaction.atomic
def mark_payout_failed(*, payout_id: int, notes: str = "") -> MerryPayout:
    p = MerryPayout.objects.select_for_update().filter(id=payout_id).first()
    if not p:
        raise NotFound("Payout not found.")
    if p.status == "PAID":
        raise BadState("Cannot fail a PAID payout.")
    p.status = "FAILED"
    if notes:
        p.notes = (notes or "").strip()[:255]
    p.save(update_fields=["status", "notes"])
    return p


# -----------------------------
# Queries / read helpers (optional)
# -----------------------------
def list_join_requests_for_merry(*, admin_user, merry_id: int, status_filter: Optional[str] = None):
    if not is_admin(admin_user):
        raise NotAllowed("Admin only.")
    merry = get_merry(merry_id)
    qs = MerryJoinRequest.objects.filter(merry=merry).select_related("user").order_by("-created_at")
    if status_filter:
        qs = qs.filter(status=status_filter)
    return qs


def list_my_join_requests(*, user):
    return MerryJoinRequest.objects.filter(user=user).select_related("merry").order_by("-created_at")


def list_my_contributions(*, user, limit: int = 200):
    return (
        MerryContribution.objects.filter(member__user=user)
        .select_related("member", "member__merry")
        .order_by("-created_at")[:limit]
    )


def list_merry_members(*, requester, merry_id: int):
    merry = get_merry(merry_id)
    is_member = MerryMember.objects.filter(merry=merry, user=requester, is_active=True).exists()
    if not is_admin(requester) and not is_member:
        raise NotAllowed("Not allowed.")
    qs = MerryMember.objects.filter(merry=merry, is_active=True).select_related("user")
    if merry.payout_order_type == "manual":
        qs = qs.order_by("payout_position", "id")
    else:
        qs = qs.order_by("id")
    return qs