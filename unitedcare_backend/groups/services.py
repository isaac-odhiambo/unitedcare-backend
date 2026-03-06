# groups/services.py (UPDATED - COMPLETE)
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError, PermissionDenied

from .models import (
    Group,
    GroupFund,
    GroupMembership,
    GroupContribution,
    GroupMemberShare,
    GroupShareHold,
)


def q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"))


def require_active_membership(group_id: int, user):
    m = GroupMembership.objects.filter(group_id=group_id, user=user, is_active=True).first()
    if not m:
        raise PermissionDenied("You are not an active member of this group.")
    return m


def require_group_admin(group_id: int, user):
    m = require_active_membership(group_id, user)
    if m.role != "ADMIN":
        raise PermissionDenied("Admin only.")
    return m


@transaction.atomic
def get_or_create_group_fund(group_id: int) -> GroupFund:
    fund = GroupFund.objects.select_for_update().filter(group_id=group_id).first()
    if fund:
        return fund
    # create if missing
    if not Group.objects.filter(id=group_id).exists():
        raise ValidationError("Group not found.")
    return GroupFund.objects.create(group_id=group_id, balance=Decimal("0.00"), reserved_amount=Decimal("0.00"))


@transaction.atomic
def get_or_create_member_share(group_id: int, user_id: int) -> GroupMemberShare:
    share = GroupMemberShare.objects.select_for_update().filter(group_id=group_id, user_id=user_id).first()
    if share:
        return share
    return GroupMemberShare.objects.create(group_id=group_id, user_id=user_id)


@transaction.atomic
def post_group_contribution(
    *,
    group_id: int,
    user,
    amount: Decimal,
    reference: str | None = None,
    note: str | None = None,
) -> dict:
    """
    Adds money to group fund + updates member share + writes contribution record.

    Call this from:
    - groups endpoint (manual)
    - payments callback for purpose=GROUP_CONTRIBUTION (MPesa)

    ✅ Updates added:
    1) Normalize reference for group contributions to: GROUP-<group_id>
    2) Idempotency guard: if reference already used, don't double-credit.
    """
    require_active_membership(group_id, user)

    amount = q2(Decimal(str(amount)))
    if amount <= 0:
        raise ValidationError("Amount must be greater than 0.")

    # ✅ UPDATED: normalize / enforce reference format
    # Always store group-level reference like your payments convention:
    #   reference = "GROUP-<group_id>"
    normalized_ref = f"GROUP-{int(group_id)}"
    if reference:
        reference = (reference or "").strip()
        # If someone passes "GROUP-12" while group_id=12 -> fine.
        # Otherwise force to normalized group ref to keep ledger joins correct.
        reference = normalized_ref if not reference.startswith("GROUP-") else reference
        if reference != normalized_ref:
            reference = normalized_ref
    else:
        reference = normalized_ref

    # ✅ UPDATED: idempotency guard (prevents double-credit on callback retries)
    # If MPesa callback retries with same reference + user + amount, we won't post twice.
    # (This is simple, but effective for your current reference scheme.)
    existing = GroupContribution.objects.filter(
        group_id=group_id,
        user=user,
        reference=reference,
        amount=amount,
    ).first()
    if existing:
        share = get_or_create_member_share(group_id, user.id)
        fund = get_or_create_group_fund(group_id)
        return {
            "message": "Contribution already posted (idempotent).",
            "contribution_id": existing.id,
            "group_id": group_id,
            "amount": str(amount),
            "group_fund_balance": str(fund.balance),
            "my_total_contributed": str(share.total_contributed),
            "my_available_share": str(share.available_share),
        }

    fund = get_or_create_group_fund(group_id)
    share = get_or_create_member_share(group_id, user.id)

    # apply
    fund.balance = q2(Decimal(fund.balance) + amount)
    fund.full_clean()
    fund.save(update_fields=["balance"])

    share.total_contributed = q2(Decimal(share.total_contributed) + amount)
    share.full_clean()
    share.save(update_fields=["total_contributed", "updated_at"])

    c = GroupContribution.objects.create(
        group_id=group_id,
        user=user,
        amount=amount,
        reference=reference,
        note=note,
        created_at=timezone.now(),
    )

    return {
        "message": "Contribution posted.",
        "contribution_id": c.id,
        "group_id": group_id,
        "amount": str(amount),
        "group_fund_balance": str(fund.balance),
        "my_total_contributed": str(share.total_contributed),
        "my_available_share": str(share.available_share),
    }


@transaction.atomic
def reserve_group_share_for_loan(
    *,
    group_id: int,
    user,           # borrower
    loan_id: int,
    amount: Decimal,
) -> GroupShareHold:
    """
    Locks member share as collateral for a GROUP loan (same group).
    This does not reduce group fund. It only locks that member's share.
    """
    require_active_membership(group_id, user)

    amount = q2(Decimal(str(amount)))
    if amount <= 0:
        raise ValidationError("Amount must be greater than 0.")

    share = get_or_create_member_share(group_id, user.id)

    if amount > share.available_share:
        raise ValidationError("Insufficient available group share to reserve.")

    # create hold
    hold = GroupShareHold.objects.create(
        group_id=group_id,
        user=user,
        loan_id=int(loan_id),
        amount=amount,
        is_active=True,
        created_at=timezone.now(),
    )

    # lock
    share.reserved_share = q2(Decimal(share.reserved_share) + amount)
    share.full_clean()
    share.save(update_fields=["reserved_share", "updated_at"])

    return hold


@transaction.atomic
def release_group_share_for_loan(*, group_id: int, loan_id: int) -> dict:
    """
    Releases ALL active holds for this loan in this group.
    Use this when loan COMPLETES/REJECTED/CANCELLED.
    """
    holds = (
        GroupShareHold.objects.select_for_update()
        .filter(group_id=group_id, loan_id=int(loan_id), is_active=True)
        .order_by("id")
    )

    if not holds.exists():
        return {"message": "No active group share holds to release."}

    # All holds should be for same user (borrower), but support multiple holds safely.
    released_total = Decimal("0.00")
    for h in holds:
        share = GroupMemberShare.objects.select_for_update().get(group_id=group_id, user_id=h.user_id)
        share.reserved_share = q2(Decimal(share.reserved_share) - Decimal(h.amount))
        if share.reserved_share < 0:
            share.reserved_share = Decimal("0.00")
        share.full_clean()
        share.save(update_fields=["reserved_share", "updated_at"])

        h.release()
        released_total = q2(released_total + Decimal(h.amount))

    return {"message": "Released group share holds.", "loan_id": int(loan_id), "released_total": str(released_total)}