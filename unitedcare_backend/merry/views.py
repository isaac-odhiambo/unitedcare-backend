# merry/views.py
# Admin-approval join flow + membership + contributions + payout requests (via payments app)
#
# Assumptions:
# - You use DRF + JWT and IsAuthenticated in settings.
# - Your User model has: phone (optional). Admin check uses is_staff/is_superuser by default.
# - Payments app already exists. This file only prepares MerryContribution/Payout records.
#
# If your admin system uses user.role == "admin", update is_admin() below.

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.utils import timezone

from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    MerryGoRound,
    MerryMember,
    MerryJoinRequest,
    MerryContribution,
    MerryPayout,
)


# -----------------------------
# Helpers
# -----------------------------
def q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"))


def is_admin(user) -> bool:
    # ✅ adjust if you use custom roles (e.g. user.role == "admin")
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def get_merry_or_404(merry_id: int) -> MerryGoRound:
    merry = MerryGoRound.objects.filter(id=merry_id).first()
    if not merry:
        raise ValidationError("Merry not found.")
    return merry


def get_member_or_404(merry_id: int, user) -> MerryMember:
    member = MerryMember.objects.filter(merry_id=merry_id, user=user, is_active=True).select_related("merry").first()
    if not member:
        raise ValidationError("You are not an active member of this merry.")
    return member


def current_week_number(merry: MerryGoRound) -> int:
    """
    Simple week_number logic:
    - Week 1 starts at merry.created_at date.
    - Each cycle_duration_weeks defines duration per payout cycle (but week_number still increments weekly).
    You can refine later if you need strict cycles.
    """
    start = merry.created_at.date()
    today = timezone.now().date()
    delta_days = (today - start).days
    if delta_days < 0:
        delta_days = 0
    return (delta_days // 7) + 1


# -----------------------------
# Merry list / create / detail
# -----------------------------
class MyMerriesView(APIView):
    """
    GET /api/merry/
    Returns:
      - merries user created (admin usually)
      - merries user is a member of
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        created = MerryGoRound.objects.filter(created_by=request.user).order_by("-id")
        memberships = MerryMember.objects.filter(user=request.user, is_active=True).select_related("merry").order_by("-id")

        created_data = [
            {
                "id": m.id,
                "name": m.name,
                "contribution_amount": str(m.contribution_amount),
                "cycle_duration_weeks": m.cycle_duration_weeks,
                "payout_order_type": m.payout_order_type,
                "next_payout_date": m.next_payout_date,
                "members_count": m.members.filter(is_active=True).count(),
                "created_at": m.created_at,
            }
            for m in created
        ]

        member_data = [
            {
                "merry_id": mm.merry_id,
                "name": mm.merry.name,
                "contribution_amount": str(mm.merry.contribution_amount),
                "cycle_duration_weeks": mm.merry.cycle_duration_weeks,
                "payout_order_type": mm.merry.payout_order_type,
                "next_payout_date": mm.merry.next_payout_date,
                "payout_position": mm.payout_position,
                "joined_at": mm.joined_at,
            }
            for mm in memberships
        ]

        return Response(
            {"created": created_data, "memberships": member_data},
            status=status.HTTP_200_OK,
        )


class CreateMerryView(APIView):
    """
    POST /api/merry/create/
    Admin creates a merry.
    Body: { name, contribution_amount, cycle_duration_weeks, payout_order_type }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        name = (request.data.get("name") or "").strip()
        contribution_amount = request.data.get("contribution_amount")
        cycle_duration_weeks = request.data.get("cycle_duration_weeks") or 1
        payout_order_type = request.data.get("payout_order_type") or "manual"
        next_payout_date = request.data.get("next_payout_date")  # optional

        if not name:
            raise ValidationError("name is required.")
        if contribution_amount is None:
            raise ValidationError("contribution_amount is required.")

        amount = q2(Decimal(str(contribution_amount)))
        if amount <= 0:
            raise ValidationError("contribution_amount must be > 0.")

        try:
            cycle_duration_weeks = int(cycle_duration_weeks)
        except Exception:
            raise ValidationError("cycle_duration_weeks must be an integer.")

        if cycle_duration_weeks <= 0:
            raise ValidationError("cycle_duration_weeks must be >= 1.")

        if payout_order_type not in ("manual", "random"):
            raise ValidationError("payout_order_type must be 'manual' or 'random'.")

        merry = MerryGoRound.objects.create(
            name=name,
            contribution_amount=amount,
            cycle_duration_weeks=cycle_duration_weeks,
            payout_order_type=payout_order_type,
            next_payout_date=next_payout_date or None,
            created_by=request.user,
        )

        return Response(
            {
                "id": merry.id,
                "name": merry.name,
                "contribution_amount": str(merry.contribution_amount),
                "cycle_duration_weeks": merry.cycle_duration_weeks,
                "payout_order_type": merry.payout_order_type,
                "next_payout_date": merry.next_payout_date,
                "created_at": merry.created_at,
            },
            status=status.HTTP_201_CREATED,
        )


class MerryDetailView(APIView):
    """
    GET /api/merry/<merry_id>/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        is_member = MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists()

        # allow view if admin or member
        if not is_admin(request.user) and not is_member:
            raise PermissionDenied("Not allowed.")

        members_count = merry.members.filter(is_active=True).count()

        return Response(
            {
                "id": merry.id,
                "name": merry.name,
                "contribution_amount": str(merry.contribution_amount),
                "cycle_duration_weeks": merry.cycle_duration_weeks,
                "payout_order_type": merry.payout_order_type,
                "next_payout_date": merry.next_payout_date,
                "members_count": members_count,
                "total_pool": str(merry.total_pool()),
                "created_by": merry.created_by_id,
                "created_at": merry.created_at,
            },
            status=status.HTTP_200_OK,
        )


# -----------------------------
# Members list
# -----------------------------
class MerryMembersView(APIView):
    """
    GET /api/merry/<merry_id>/members/
    Admin or member can view.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        is_member = MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists()

        if not is_admin(request.user) and not is_member:
            raise PermissionDenied("Not allowed.")

        members = MerryMember.objects.filter(merry=merry, is_active=True).select_related("user").order_by("payout_position", "id")

        data = [
            {
                "id": m.id,
                "user_id": m.user_id,
                "username": getattr(m.user, "username", None),
                "phone": getattr(m.user, "phone", None),
                "payout_position": m.payout_position,
                "joined_at": m.joined_at,
            }
            for m in members
        ]
        return Response(data, status=status.HTTP_200_OK)


# -----------------------------
# Join requests (member -> admin approval)
# -----------------------------
class RequestToJoinMerryView(APIView):
    """
    POST /api/merry/<merry_id>/join/request/
    Body: { note?: "..." }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)

        if MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists():
            raise ValidationError("You are already a member of this merry.")

        note = (request.data.get("note") or "").strip()[:255]

        existing = MerryJoinRequest.objects.filter(merry=merry, user=request.user).first()
        if existing:
            if existing.status == "PENDING":
                return Response(
                    {"message": "Join request already pending.", "request_id": existing.id, "status": existing.status},
                    status=status.HTTP_200_OK,
                )
            # If previously rejected/cancelled/approved, allow re-request by resetting
            existing.status = "PENDING"
            existing.note = note
            existing.reviewed_by = None
            existing.reviewed_at = None
            existing.created_at = timezone.now()
            existing.full_clean()
            existing.save(update_fields=["status", "note", "reviewed_by", "reviewed_at", "created_at"])
            return Response(
                {"message": "Join request re-submitted.", "request_id": existing.id, "status": existing.status},
                status=status.HTTP_201_CREATED,
            )

        jr = MerryJoinRequest(merry=merry, user=request.user, status="PENDING", note=note)
        jr.full_clean()
        jr.save()

        return Response(
            {"message": "Join request submitted.", "request_id": jr.id, "status": jr.status},
            status=status.HTTP_201_CREATED,
        )


class CancelJoinRequestView(APIView):
    """
    POST /api/merry/join/requests/<request_id>/cancel/
    Member cancels their own pending request.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, request_id: int):
        jr = MerryJoinRequest.objects.filter(id=request_id, user=request.user).first()
        if not jr:
            raise ValidationError("Join request not found.")
        jr.cancel(request.user)
        return Response({"message": "Join request cancelled."}, status=status.HTTP_200_OK)


class MyJoinRequestsView(APIView):
    """
    GET /api/merry/join/requests/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = MerryJoinRequest.objects.filter(user=request.user).select_related("merry").order_by("-created_at")
        data = [
            {
                "id": r.id,
                "merry_id": r.merry_id,
                "merry_name": r.merry.name,
                "status": r.status,
                "note": r.note,
                "created_at": r.created_at,
                "reviewed_at": r.reviewed_at,
            }
            for r in qs
        ]
        return Response(data, status=status.HTTP_200_OK)


class AdminListJoinRequestsView(APIView):
    """
    GET /api/merry/<merry_id>/join/requests/?status=PENDING
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")
        merry = get_merry_or_404(merry_id)

        status_filter = request.query_params.get("status")
        qs = MerryJoinRequest.objects.filter(merry=merry).select_related("user").order_by("-created_at")
        if status_filter:
            qs = qs.filter(status=status_filter)

        data = [
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": getattr(r.user, "username", None),
                "phone": getattr(r.user, "phone", None),
                "status": r.status,
                "note": r.note,
                "created_at": r.created_at,
            }
            for r in qs
        ]
        return Response(data, status=status.HTTP_200_OK)


class AdminApproveJoinRequestView(APIView):
    """
    POST /api/merry/join/requests/<request_id>/approve/
    Creates MerryMember and auto assigns payout_position if manual.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, request_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        jr = MerryJoinRequest.objects.select_for_update().select_related("merry", "user").filter(id=request_id).first()
        if not jr:
            raise ValidationError("Join request not found.")

        member = jr.approve(request.user)
        return Response(
            {
                "message": "Join request approved.",
                "member_id": member.id,
                "merry_id": member.merry_id,
                "user_id": member.user_id,
                "payout_position": member.payout_position,
            },
            status=status.HTTP_200_OK,
        )


class AdminRejectJoinRequestView(APIView):
    """
    POST /api/merry/join/requests/<request_id>/reject/
    Body: { note?: "reason" }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, request_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        jr = MerryJoinRequest.objects.select_related("merry", "user").filter(id=request_id).first()
        if not jr:
            raise ValidationError("Join request not found.")

        note = (request.data.get("note") or "").strip()
        jr.reject(request.user, note=note)

        return Response({"message": "Join request rejected."}, status=status.HTTP_200_OK)


# -----------------------------
# Contributions
# -----------------------------
class MyMerryContributionsView(APIView):
    """
    GET /api/merry/contributions/
    Shows contributions for logged in user (across all merries).
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = (
            MerryContribution.objects.filter(member__user=request.user)
            .select_related("member", "member__merry")
            .order_by("-created_at")[:200]
        )

        data = [
            {
                "id": c.id,
                "merry_id": c.member.merry_id,
                "merry_name": c.member.merry.name,
                "member_id": c.member_id,
                "week_number": c.week_number,
                "amount": str(c.amount),
                "status": c.status,
                "paid_at": c.paid_at,
                "mpesa_receipt_number": c.mpesa_receipt_number,
                "created_at": c.created_at,
            }
            for c in qs
        ]
        return Response(data, status=status.HTTP_200_OK)


class CreateContributionIntentView(APIView):
    """
    POST /api/merry/<merry_id>/contribute/
    Body: { week_number?: int, reference?: str }

    This creates a MerryContribution record (PENDING).
    Then your Payments app should:
      - initiate STK push
      - attach MpesaTransaction.target_object -> this contribution (GenericForeignKey)
      - on callback success: mark contribution PAID and create ledger entries

    This endpoint only creates the contribution record (and optionally returns enough
    info for the frontend to call Payments STK endpoint).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, merry_id: int):
        member = get_member_or_404(merry_id, request.user)
        merry = member.merry

        week_number = request.data.get("week_number")
        if week_number is None:
            week_number = current_week_number(merry)
        try:
            week_number = int(week_number)
        except Exception:
            raise ValidationError("week_number must be an integer.")
        if week_number <= 0:
            raise ValidationError("week_number must be >= 1.")

        # prevent duplicate per member/week (model constraint exists, but we do friendly message)
        exists = MerryContribution.objects.filter(member=member, week_number=week_number).first()
        if exists:
            return Response(
                {
                    "message": "Contribution already exists for this week.",
                    "contribution_id": exists.id,
                    "status": exists.status,
                },
                status=status.HTTP_200_OK,
            )

        contribution = MerryContribution.objects.create(
            member=member,
            week_number=week_number,
            amount=merry.contribution_amount,
            status="PENDING",
        )

        # Return details the frontend can use to call payments/STK endpoint
        return Response(
            {
                "message": "Contribution intent created.",
                "contribution_id": contribution.id,
                "merry_id": merry.id,
                "amount": str(contribution.amount),
                "week_number": contribution.week_number,
                "status": contribution.status,
                # frontend can now call payments endpoint like:
                # POST /api/payments/stk/ with target_type="MerryContribution" target_id=contribution.id
            },
            status=status.HTTP_201_CREATED,
        )


class MarkContributionPaidView(APIView):
    """
    POST /api/merry/contributions/<contribution_id>/mark-paid/
    OPTIONAL: normally Payments callback should do this.
    Admin only.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, contribution_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        c = MerryContribution.objects.select_related("member", "member__merry").filter(id=contribution_id).first()
        if not c:
            raise ValidationError("Contribution not found.")
        if c.status == "PAID":
            return Response({"message": "Already PAID."}, status=status.HTTP_200_OK)

        c.status = "PAID"
        c.paid_at = timezone.now()
        c.save(update_fields=["status", "paid_at"])

        return Response({"message": "Contribution marked PAID."}, status=status.HTTP_200_OK)


# -----------------------------
# Payout schedule + records
# -----------------------------
class MerryPayoutScheduleView(APIView):
    """
    GET /api/merry/<merry_id>/payouts/schedule/
    Admin or member can view.

    Very simple schedule:
      - list members ordered by payout_position (manual) or join order
      - show who would be next (based on week_number and existing payouts)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        is_member = MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists()
        if not is_admin(request.user) and not is_member:
            raise PermissionDenied("Not allowed.")

        members_qs = MerryMember.objects.filter(merry=merry, is_active=True).select_related("user")
        if merry.payout_order_type == "manual":
            members_qs = members_qs.order_by("payout_position", "id")
        else:
            members_qs = members_qs.order_by("id")

        week = current_week_number(merry)

        existing = MerryPayout.objects.filter(merry=merry).values_list("week_number", flat=True)
        existing_weeks = set(existing)

        data_members = [
            {
                "member_id": m.id,
                "user_id": m.user_id,
                "username": getattr(m.user, "username", None),
                "phone": getattr(m.user, "phone", None),
                "payout_position": m.payout_position,
            }
            for m in members_qs
        ]

        return Response(
            {
                "merry": {
                    "id": merry.id,
                    "name": merry.name,
                    "payout_order_type": merry.payout_order_type,
                    "contribution_amount": str(merry.contribution_amount),
                    "members_count": merry.members.filter(is_active=True).count(),
                },
                "current_week_number": week,
                "weeks_with_payouts": sorted(list(existing_weeks)),
                "members": data_members,
            },
            status=status.HTTP_200_OK,
        )


class CreatePayoutView(APIView):
    """
    POST /api/merry/<merry_id>/payouts/create/
    Admin creates a payout record (SCHEDULED/PROCESSING).

    NOTE:
    - Actual money sending should be handled via Payments app WithdrawalRequest.
    - This only records intention.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, merry_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        merry = get_merry_or_404(merry_id)

        member_id = request.data.get("member_id")
        amount = request.data.get("amount")
        week_number = request.data.get("week_number")

        if not member_id:
            raise ValidationError("member_id is required.")
        if amount is None:
            raise ValidationError("amount is required.")

        member = MerryMember.objects.filter(id=member_id, merry=merry, is_active=True).first()
        if not member:
            raise ValidationError("Member not found in this merry.")

        if week_number is None:
            week_number = current_week_number(merry)
        try:
            week_number = int(week_number)
        except Exception:
            raise ValidationError("week_number must be an integer.")
        if week_number <= 0:
            raise ValidationError("week_number must be >= 1.")

        amount = q2(Decimal(str(amount)))
        if amount <= 0:
            raise ValidationError("amount must be > 0.")

        # prevent duplicate per merry/week
        if MerryPayout.objects.filter(merry=merry, week_number=week_number).exists():
            raise ValidationError("A payout already exists for this merry and week.")

        payout = MerryPayout.objects.create(
            merry=merry,
            member=member,
            week_number=week_number,
            amount=amount,
            status="SCHEDULED",
            notes=(request.data.get("notes") or "")[:255],
        )

        return Response(
            {
                "message": "Payout record created.",
                "payout_id": payout.id,
                "status": payout.status,
                "merry_id": merry.id,
                "member_id": member.id,
                "amount": str(payout.amount),
                "week_number": payout.week_number,
                # frontend/admin can now create a WithdrawalRequest in payments app linked to this merry/payout
            },
            status=status.HTTP_201_CREATED,
        )


class MarkPayoutPaidView(APIView):
    """
    POST /api/merry/payouts/<payout_id>/mark-paid/
    Admin only.
    Usually called by Payments callback after successful B2C.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, payout_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        p = MerryPayout.objects.select_related("merry", "member").filter(id=payout_id).first()
        if not p:
            raise ValidationError("Payout not found.")
        if p.status == "PAID":
            return Response({"message": "Already PAID."}, status=status.HTTP_200_OK)

        p.status = "PAID"
        p.paid_at = timezone.now()
        p.save(update_fields=["status", "paid_at"])

        return Response({"message": "Payout marked PAID."}, status=status.HTTP_200_OK)