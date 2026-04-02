# groups/services.py
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import (
    Group,
    GroupContribution,
    GroupFund,
    GroupMemberShare,
    GroupMembership,
    GroupShareHold,
)


def q2(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def is_system_admin(user) -> bool:
    """
    System-level admin check.
    Supports:
    - Django superuser/staff
    - custom is_admin flag
    - custom role == 'admin'
    """
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    if getattr(user, "is_admin", False):
        return True
    if getattr(user, "role", None) == "admin":
        return True
    return False


def get_group_or_404(group_id: int) -> Group:
    group = Group.objects.filter(id=group_id).first()
    if not group:
        raise ValidationError("Group not found.")
    return group


def require_active_group(group_id: int) -> Group:
    group = get_group_or_404(group_id)
    if not group.is_active:
        raise ValidationError("This group is inactive.")
    return group


def require_active_membership(group_id: int, user) -> GroupMembership:
    """
    Require the user to be an active member of the given group.
    """
    require_active_group(group_id)

    membership = GroupMembership.objects.filter(
        group_id=group_id,
        user=user,
        is_active=True,
    ).first()

    if not membership:
        raise PermissionDenied("You are not an active member of this group.")

    return membership


def require_group_admin(group_id: int, user) -> GroupMembership:
    """
    Require group admin role.
    System admin is treated as allowed, but if not a group member,
    we still return a normal membership object only when it exists.
    """
    if is_system_admin(user):
        membership = GroupMembership.objects.filter(
            group_id=group_id,
            user=user,
            is_active=True,
        ).first()
        if membership:
            return membership

        # System admin may manage the group even without a membership row.
        group = require_active_group(group_id)

        class _SystemAdminMembershipProxy:
            group_id = group.id
            user_id = user.id
            role = "ADMIN"
            is_active = True

        return _SystemAdminMembershipProxy()

    membership = require_active_membership(group_id, user)
    if membership.role != "ADMIN":
        raise PermissionDenied("Admin only.")
    return membership


@transaction.atomic
def get_or_create_group_fund(group_id: int) -> GroupFund:
    """
    Create or fetch the group pooled fund.
    """
    require_active_group(group_id)

    fund = (
        GroupFund.objects.select_for_update()
        .filter(group_id=group_id)
        .first()
    )
    if fund:
        return fund

    return GroupFund.objects.create(
        group_id=group_id,
        balance=Decimal("0.00"),
        reserved_amount=Decimal("0.00"),
    )


@transaction.atomic
def get_or_create_member_share(group_id: int, user_id: int) -> GroupMemberShare:
    """
    Create or fetch a member's contribution share row.
    Usually called only for members, approved joiners, or contribution posting.
    """
    group = require_active_group(group_id)

    share = (
        GroupMemberShare.objects.select_for_update()
        .filter(group_id=group_id, user_id=user_id)
        .first()
    )
    if share:
        return share

    return GroupMemberShare.objects.create(
        group=group,
        user_id=user_id,
        total_contributed=Decimal("0.00"),
        reserved_share=Decimal("0.00"),
    )


def normalize_group_reference(group_id: int, reference: str | None = None) -> str:
    """
    Normalized group reference used for grouped accounting/ledger matching.

    Rules:
    - If no reference is supplied, use GROUP-<group_id>
    - If a caller supplies a GROUP-* reference, normalize it back to GROUP-<group_id>
    - Other custom references (like MPESA code / receipt) are allowed
    """
    normalized_group_ref = f"GROUP-{int(group_id)}"

    if not reference:
        return normalized_group_ref

    reference = reference.strip()
    if not reference:
        return normalized_group_ref

    if reference.startswith("GROUP-"):
        return normalized_group_ref

    return reference


@transaction.atomic
def post_group_contribution(
    *,
    group_id: int,
    user,
    amount: Decimal,
    reference: str | None = None,
    note: str | None = None,
    source: str = "MANUAL",
) -> dict:
    """
    Adds money to group fund + updates member share + writes contribution record.

    Call this from:
    - groups endpoint (manual)
    - payments callback for purpose=GROUP_CONTRIBUTION (MPesa)

    Practical rules:
    - contributor must be active member
    - group must be active
    - if group.requires_contributions is False, contribution is still allowed
      because many welfare/community groups accept optional contributions
    - idempotency:
        * if non-group custom reference exists for same group, do not double-credit
        * if reference is normalized GROUP-<id>, only exact same user+amount+reference
          is treated as duplicate to avoid blocking legitimate repeated manual deposits
    """
    membership = require_active_membership(group_id, user)
    group = membership.group

    amount = q2(amount)
    if amount <= 0:
        raise ValidationError("Amount must be greater than 0.")

    note = (note or "").strip() or None
    source = (source or "MANUAL").strip().upper()
    allowed_sources = {"MANUAL", "MPESA", "BANK", "OTHER"}
    if source not in allowed_sources:
        source = "MANUAL"

    normalized_reference = normalize_group_reference(group_id, reference)
    default_group_ref = f"GROUP-{int(group_id)}"

    # Idempotency logic
    # Case 1: external specific receipt/reference (e.g. MPESA code) -> unique per group
    if normalized_reference != default_group_ref:
        existing = GroupContribution.objects.filter(
            group_id=group_id,
            reference=normalized_reference,
        ).first()
        if existing:
            share = get_or_create_member_share(group_id, user.id)
            fund = get_or_create_group_fund(group_id)
            return {
                "message": "Contribution already posted (idempotent).",
                "contribution_id": existing.id,
                "group_id": group_id,
                "amount": str(existing.amount),
                "reference": existing.reference,
                "source": getattr(existing, "source", source),
                "group_fund_balance": str(fund.balance),
                "my_total_contributed": str(share.total_contributed),
                "my_available_share": str(share.available_share),
            }

    # Case 2: generic group reference -> dedupe only same user + amount + reference close enough
    else:
        existing = GroupContribution.objects.filter(
            group_id=group_id,
            user=user,
            amount=amount,
            reference=normalized_reference,
        ).order_by("-id").first()

        if existing:
            share = get_or_create_member_share(group_id, user.id)
            fund = get_or_create_group_fund(group_id)
            return {
                "message": "Contribution already posted (idempotent).",
                "contribution_id": existing.id,
                "group_id": group_id,
                "amount": str(existing.amount),
                "reference": existing.reference,
                "source": getattr(existing, "source", source),
                "group_fund_balance": str(fund.balance),
                "my_total_contributed": str(share.total_contributed),
                "my_available_share": str(share.available_share),
            }

    fund = get_or_create_group_fund(group_id)
    share = get_or_create_member_share(group_id, user.id)

    fund.balance = q2(fund.balance + amount)
    fund.full_clean()
    fund.save(update_fields=["balance"])

    share.total_contributed = q2(share.total_contributed + amount)
    share.full_clean()
    share.save(update_fields=["total_contributed", "updated_at"])

    contribution = GroupContribution.objects.create(
        group_id=group_id,
        user=user,
        amount=amount,
        source=source,
        reference=normalized_reference,
        note=note,
        created_at=timezone.now(),
    )

    return {
        "message": "Contribution posted.",
        "contribution_id": contribution.id,
        "group_id": group_id,
        "amount": str(amount),
        "reference": contribution.reference,
        "source": contribution.source,
        "group_fund_balance": str(fund.balance),
        "my_total_contributed": str(share.total_contributed),
        "my_available_share": str(share.available_share),
    }


@transaction.atomic
def reserve_group_share_for_loan(
    *,
    group_id: int,
    user,
    loan_id: int,
    amount: Decimal,
) -> GroupShareHold:
    """
    Locks member share as collateral for a GROUP loan.
    This does not reduce the group fund.
    It only locks that member's available share.
    """
    require_active_membership(group_id, user)

    amount = q2(amount)
    if amount <= 0:
        raise ValidationError("Amount must be greater than 0.")

    share = get_or_create_member_share(group_id, user.id)

    if amount > share.available_share:
        raise ValidationError("Insufficient available group share to reserve.")

    hold = GroupShareHold.objects.create(
        group_id=group_id,
        user=user,
        loan_id=int(loan_id),
        amount=amount,
        is_active=True,
        created_at=timezone.now(),
    )

    share.reserved_share = q2(share.reserved_share + amount)
    share.full_clean()
    share.save(update_fields=["reserved_share", "updated_at"])

    return hold


@transaction.atomic
def release_group_share_for_loan(*, group_id: int, loan_id: int) -> dict:
    """
    Releases all active holds for this loan in this group.
    Use when loan is completed, rejected, cancelled, or otherwise closed.
    """
    holds = (
        GroupShareHold.objects.select_for_update()
        .filter(group_id=group_id, loan_id=int(loan_id), is_active=True)
        .order_by("id")
    )

    if not holds.exists():
        return {
            "message": "No active group share holds to release.",
            "loan_id": int(loan_id),
            "released_total": "0.00",
        }

    released_total = Decimal("0.00")

    for hold in holds:
        share = GroupMemberShare.objects.select_for_update().get(
            group_id=group_id,
            user_id=hold.user_id,
        )

        share.reserved_share = q2(share.reserved_share - hold.amount)
        if share.reserved_share < Decimal("0.00"):
            share.reserved_share = Decimal("0.00")

        share.full_clean()
        share.save(update_fields=["reserved_share", "updated_at"])

        hold.release()
        released_total = q2(released_total + hold.amount)

    return {
        "message": "Released group share holds.",
        "loan_id": int(loan_id),
        "released_total": str(released_total),
    }

@transaction.atomic
def apply_mpesa_contribution(*, user, amount: Decimal, mpesa_tx, reference: str = "") -> dict:
    """
    Apply MPESA group contribution.

    Supports:
    - GROUP1 / GROUP-1 / GRP1  (old format)
    - UN1 / WF12 / MG7         (new format: <group_code><user_id>)
    """

    if not user:
        raise ValidationError("MPESA transaction has no linked user.")

    raw_reference = (reference or getattr(mpesa_tx, "reference", "") or "").strip()
    if not raw_reference:
        raise ValidationError("Missing group payment reference.")

    ref_upper = raw_reference.upper().replace(" ", "")

    group_id = None
    target_user = user  # default (in-app payments)

    # ======================================================
    # 1. OLD FORMAT SUPPORT (GROUP1 / GRP1)
    # ======================================================
    if ref_upper.startswith("GROUP-"):
        suffix = ref_upper.replace("GROUP-", "", 1)
        if suffix.isdigit():
            group_id = int(suffix)

    elif ref_upper.startswith("GROUP"):
        suffix = ref_upper.replace("GROUP", "", 1)
        if suffix.isdigit():
            group_id = int(suffix)

    elif ref_upper.startswith("GRP"):
        suffix = ref_upper.replace("GRP", "", 1)
        if suffix.isdigit():
            group_id = int(suffix)

    # ======================================================
    # 2. NEW FORMAT SUPPORT (UN1, WF12, MG7)
    # ======================================================
    else:
        letters = ""
        digits = ""

        for ch in ref_upper:
            if ch.isalpha():
                if digits:
                    # letters after digits → invalid
                    raise ValidationError(f"Invalid reference format: {raw_reference}")
                letters += ch
            elif ch.isdigit():
                digits += ch
            else:
                raise ValidationError(f"Invalid characters in reference: {raw_reference}")

        if not letters or not digits:
            raise ValidationError(f"Invalid reference format: {raw_reference}")

        # find group by payment_code
        group = Group.objects.filter(payment_code=letters).first()
        if not group:
            raise ValidationError(f"Group with code '{letters}' not found.")

        group_id = group.id

        # override user using parsed user_id
        from django.contrib.auth import get_user_model
        User = get_user_model()

        parsed_user = User.objects.filter(id=int(digits)).first()
        if not parsed_user:
            raise ValidationError(f"User with id {digits} not found.")

        target_user = parsed_user

    # ======================================================
    # FINAL VALIDATION
    # ======================================================
    if not group_id:
        raise ValidationError(f"Invalid group reference: {raw_reference}")

    # ensure user is a member
    require_active_membership(group_id, target_user)

    # ======================================================
    # CREATE UNIQUE REFERENCE
    # ======================================================
    receipt = getattr(mpesa_tx, "mpesa_receipt_number", None)
    tx_id = getattr(mpesa_tx, "id", None)

    unique_reference = receipt or (f"MPESA_TX#{tx_id}" if tx_id else raw_reference)

    note = f"MPESA contribution via transaction #{tx_id}" if tx_id else "MPESA contribution"

    # ======================================================
    # POST CONTRIBUTION
    # ======================================================
    return post_group_contribution(
        group_id=group_id,
        user=target_user,
        amount=amount,
        reference=unique_reference,
        note=note,
        source="MPESA",
    )