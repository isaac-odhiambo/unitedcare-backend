# merry/views.py
# UPDATED — Admin-approval join flow + seats/shares + slot-based dues + payments + allocations + seat-based payouts

from __future__ import annotations

from decimal import Decimal
from typing import Optional, List, Dict, Any

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

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

# ==========================================
# Helpers
# ==========================================
def q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"))


def is_admin(user) -> bool:
    # adjust if you use custom roles (e.g. user.role == "admin")
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def get_merry_or_404(merry_id: int) -> MerryGoRound:
    merry = MerryGoRound.objects.filter(id=merry_id).first()
    if not merry:
        raise ValidationError("Merry not found.")
    return merry


def get_member_or_404(merry_id: int, user) -> MerryMember:
    member = (
        MerryMember.objects.filter(merry_id=merry_id, user=user, is_active=True)
        .select_related("merry", "user")
        .first()
    )
    if not member:
        raise ValidationError("You are not an active member of this merry.")
    return member


def current_period_key(merry: MerryGoRound) -> str:
    # uses model helper
    return merry.current_period_key()


def payouts_per_period(merry: MerryGoRound) -> int:
    n = int(getattr(merry, "payouts_per_period", 1) or 1)
    return max(1, n)


def validate_slot(merry: MerryGoRound, slot_no: int) -> None:
    limit = payouts_per_period(merry)
    if slot_no < 1 or slot_no > limit:
        raise ValidationError(f"slot_no must be between 1 and {limit}.")


def next_available_slot(merry: MerryGoRound, period_key: str) -> int:
    limit = payouts_per_period(merry)
    used = set(
        MerryPayout.objects.filter(merry=merry, period_key=period_key)
        .values_list("slot_no", flat=True)
    )
    for s in range(1, limit + 1):
        if s not in used:
            return s
    raise ValidationError(f"Payout slots are full for period {period_key}. Max slots: {limit}.")


# ==========================================
# Allocation engine (slot-first, seat-aware)
# ==========================================
def _next_week_period_key(period_key: str) -> str:
    # period_key: YYYY-W##
    try:
        year = int(period_key[:4])
        week = int(period_key.split("-W")[1])
    except Exception:
        raise ValidationError("Invalid WEEKLY period_key format. Expected YYYY-W##")

    from datetime import date, timedelta

    d = date.fromisocalendar(year, week, 1) + timedelta(days=7)
    y, w, _ = d.isocalendar()
    return f"{y:04d}-W{w:02d}"


def _next_month_period_key(period_key: str) -> str:
    # period_key: YYYY-MM
    try:
        year = int(period_key[:4])
        month = int(period_key.split("-")[1])
    except Exception:
        raise ValidationError("Invalid MONTHLY period_key format. Expected YYYY-MM")

    month += 1
    if month == 13:
        month = 1
        year += 1
    return f"{year:04d}-{month:02d}"


def _next_period_key(merry: MerryGoRound, period_key: str) -> str:
    if merry.payout_frequency == "MONTHLY":
        return _next_month_period_key(period_key)
    return _next_week_period_key(period_key)


@transaction.atomic
def _ensure_dues_for_member_period(merry: MerryGoRound, member: MerryMember, period_key: str) -> None:
    """
    Ensures dues exist for ALL active seats of the member for a given period for all slots.
    """
    due_amt = merry.contribution_amount or Decimal("0")
    active_seats = list(
        MerrySeat.objects.select_for_update()
        .filter(merry=merry, member=member, is_active=True)
        .order_by("seat_no", "id")
    )
    for seat in active_seats:
        for slot_no in range(1, merry.payouts_per_period + 1):
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


@transaction.atomic
def allocate_payment(payment_id: int) -> MerryPayment:
    """
    Allocates CONFIRMED payment amount across dues:
    Order: earliest period -> slot 1..N -> seat_no 1..N (stable)
    Supports partial dues and carry-forward to next slots/periods.
    """
    payment = (
        MerryPayment.objects.select_for_update()
        .select_related("merry", "beneficiary_member", "beneficiary_member__user")
        .get(id=payment_id)
    )

    if payment.status != "CONFIRMED":
        raise ValidationError("Payment must be CONFIRMED before allocation.")

    merry = payment.merry
    member = payment.beneficiary_member

    if member.merry_id != merry.id:
        raise ValidationError("Payment beneficiary does not belong to this merry.")

    remaining = payment.amount or Decimal("0")
    if remaining <= 0:
        raise ValidationError("Payment amount must be > 0.")

    period_key = payment.period_key
    safety = 0

    while remaining > 0:
        safety += 1
        if safety > 2000:
            raise ValidationError("Allocation safety limit reached (period loop).")

        # Create dues if missing for this period for all seats
        _ensure_dues_for_member_period(merry, member, period_key)

        # Select unpaid/partial dues for this member in this period
        # Ordered: slot -> seat_no
        dues = list(
            MerryContributionDue.objects.select_for_update()
            .filter(
                merry=merry,
                seat__member=member,
                period_key=period_key,
                seat__is_active=True,
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

            # upsert allocation row
            a, _ = MerryPaymentAllocation.objects.get_or_create(
                payment=payment,
                due=due,
                defaults={"amount_allocated": Decimal("0")},
            )
            a.amount_allocated = (a.amount_allocated or Decimal("0")) + alloc
            a.full_clean()
            a.save(update_fields=["amount_allocated"])

            # update due
            due.paid_amount = (due.paid_amount or Decimal("0")) + alloc
            due.recalc_status()
            due.save(update_fields=["paid_amount", "status", "updated_at"])

            remaining -= alloc
            if remaining <= 0:
                break

        if remaining <= 0:
            break

        # move forward if nothing left to fill in this period or fully paid
        if not any_needed:
            period_key = _next_period_key(merry, period_key)
            continue

        # if we filled everything in this period and still have money, go next period
        period_key = _next_period_key(merry, period_key)

    return payment


# ==========================================
# Merry list / create / detail
# ==========================================
class MyMerriesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        created = MerryGoRound.objects.filter(created_by=request.user).order_by("-id")

        memberships = (
            MerryMember.objects.filter(user=request.user, is_active=True)
            .select_related("merry")
            .order_by("-id")
        )

        created_data = [
            {
                "id": m.id,
                "name": m.name,
                "contribution_amount": str(m.contribution_amount),
                "cycle_duration_weeks": m.cycle_duration_weeks,
                "payout_order_type": m.payout_order_type,
                "next_payout_date": m.next_payout_date,
                "payout_frequency": m.payout_frequency,
                "payouts_per_period": m.payouts_per_period,
                "members_count": m.members.filter(is_active=True).count(),
                "seats_count": m.seats.filter(is_active=True).count(),
                "created_at": m.created_at,
            }
            for m in created
        ]

        member_data = []
        for mm in memberships:
            seats_count = mm.seats.filter(is_active=True).count()
            member_data.append(
                {
                    "merry_id": mm.merry_id,
                    "name": mm.merry.name,
                    "contribution_amount": str(mm.merry.contribution_amount),
                    "cycle_duration_weeks": mm.merry.cycle_duration_weeks,
                    "payout_order_type": mm.merry.payout_order_type,
                    "next_payout_date": mm.merry.next_payout_date,
                    "payout_frequency": mm.merry.payout_frequency,
                    "payouts_per_period": mm.merry.payouts_per_period,
                    "joined_at": mm.joined_at,
                    "seats_count": seats_count,
                }
            )

        return Response({"created": created_data, "memberships": member_data}, status=status.HTTP_200_OK)


class CreateMerryView(APIView):
    """
    POST /api/merry/create/
    Admin creates a merry.
    Body:
    {
      name, contribution_amount, cycle_duration_weeks, payout_order_type,
      next_payout_date?,
      payout_frequency? ("WEEKLY"|"MONTHLY"),
      payouts_per_period? (int)
    }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        name = (request.data.get("name") or "").strip()
        contribution_amount = request.data.get("contribution_amount")
        cycle_duration_weeks = request.data.get("cycle_duration_weeks") or 1
        payout_order_type = (request.data.get("payout_order_type") or "manual").strip()
        next_payout_date = request.data.get("next_payout_date")

        payout_frequency = (request.data.get("payout_frequency") or "WEEKLY").upper()
        payouts_pp = request.data.get("payouts_per_period") or 1

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

        if payout_frequency not in ("WEEKLY", "MONTHLY"):
            raise ValidationError("payout_frequency must be 'WEEKLY' or 'MONTHLY'.")

        try:
            payouts_pp = int(payouts_pp)
        except Exception:
            raise ValidationError("payouts_per_period must be an integer.")
        if payouts_pp < 1 or payouts_pp > 14:
            raise ValidationError("payouts_per_period must be between 1 and 14.")

        merry = MerryGoRound.objects.create(
            name=name,
            contribution_amount=amount,
            cycle_duration_weeks=cycle_duration_weeks,
            payout_order_type=payout_order_type,
            next_payout_date=next_payout_date or None,
            created_by=request.user,
            payout_frequency=payout_frequency,
            payouts_per_period=payouts_pp,
        )

        return Response(
            {
                "id": merry.id,
                "name": merry.name,
                "contribution_amount": str(merry.contribution_amount),
                "cycle_duration_weeks": merry.cycle_duration_weeks,
                "payout_order_type": merry.payout_order_type,
                "next_payout_date": merry.next_payout_date,
                "payout_frequency": merry.payout_frequency,
                "payouts_per_period": merry.payouts_per_period,
                "created_at": merry.created_at,
            },
            status=status.HTTP_201_CREATED,
        )


class MerryDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        is_member = MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists()

        if not is_admin(request.user) and not is_member:
            raise PermissionDenied("Not allowed.")

        members_count = merry.members.filter(is_active=True).count()
        seats_count = merry.seats.filter(is_active=True).count()

        return Response(
            {
                "id": merry.id,
                "name": merry.name,
                "contribution_amount": str(merry.contribution_amount),
                "cycle_duration_weeks": merry.cycle_duration_weeks,
                "payout_order_type": merry.payout_order_type,
                "next_payout_date": merry.next_payout_date,
                "payout_frequency": merry.payout_frequency,
                "payouts_per_period": merry.payouts_per_period,
                "members_count": members_count,
                "seats_count": seats_count,
                "total_pool_per_slot": str(merry.total_pool_per_slot()),
                "total_pool_per_period": str(merry.total_pool_per_period()),
                "created_by": merry.created_by_id,
                "created_at": merry.created_at,
            },
            status=status.HTTP_200_OK,
        )


# ==========================================
# Members & Seats
# ==========================================
class MerryMembersView(APIView):
    """
    GET /api/merry/<merry_id>/members/
    Shows members + seats_count.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        is_member = MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists()
        if not is_admin(request.user) and not is_member:
            raise PermissionDenied("Not allowed.")

        qs = MerryMember.objects.filter(merry=merry, is_active=True).select_related("user").order_by("id")
        data = []
        for m in qs:
            data.append(
                {
                    "member_id": m.id,
                    "user_id": m.user_id,
                    "username": getattr(m.user, "username", None),
                    "phone": getattr(m.user, "phone", None),
                    "joined_at": m.joined_at,
                    "seats_count": m.seats.filter(is_active=True).count(),
                }
            )
        return Response(data, status=status.HTTP_200_OK)


class MerrySeatsView(APIView):
    """
    GET /api/merry/<merry_id>/seats/
    Admin/member can view seats & payout positions.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        is_member = MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists()
        if not is_admin(request.user) and not is_member:
            raise PermissionDenied("Not allowed.")

        qs = MerrySeat.objects.filter(merry=merry, is_active=True).select_related("member", "member__user")

        if merry.payout_order_type == "manual":
            qs = qs.order_by("payout_position", "id")
        else:
            qs = qs.order_by("id")

        data = [
            {
                "seat_id": s.id,
                "member_id": s.member_id,
                "user_id": s.member.user_id,
                "username": getattr(s.member.user, "username", None),
                "phone": getattr(s.member.user, "phone", None),
                "seat_no": s.seat_no,
                "payout_position": s.payout_position,
                "created_at": s.created_at,
            }
            for s in qs
        ]
        return Response(data, status=status.HTTP_200_OK)


# ==========================================
# Slot config (admin)
# ==========================================
class SlotConfigView(APIView):
    """
    GET /api/merry/<merry_id>/slots/
    POST /api/merry/<merry_id>/slots/  (admin)
      body: [{slot_no:1, weekday:0}, {slot_no:2, weekday:4}]
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        is_member = MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists()
        if not is_admin(request.user) and not is_member:
            raise PermissionDenied("Not allowed.")

        rows = MerrySlotConfig.objects.filter(merry=merry).order_by("slot_no")
        return Response(
            [
                {"slot_no": r.slot_no, "weekday": r.weekday, "weekday_name": r.get_weekday_display()}
                for r in rows
            ],
            status=status.HTTP_200_OK,
        )

    @transaction.atomic
    def post(self, request, merry_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")
        merry = get_merry_or_404(merry_id)

        items = request.data
        if not isinstance(items, list) or not items:
            raise ValidationError("Body must be a non-empty list of {slot_no, weekday} objects.")

        # Validate all slot_no once
        seen = set()
        for it in items:
            try:
                slot_no = int(it.get("slot_no"))
                weekday = int(it.get("weekday"))
            except Exception:
                raise ValidationError("slot_no and weekday must be integers.")
            validate_slot(merry, slot_no)
            if weekday < 0 or weekday > 6:
                raise ValidationError("weekday must be 0..6 (Mon..Sun).")
            if slot_no in seen:
                raise ValidationError("Duplicate slot_no in payload.")
            seen.add(slot_no)

        # Upsert
        for it in items:
            slot_no = int(it["slot_no"])
            weekday = int(it["weekday"])
            obj, _ = MerrySlotConfig.objects.get_or_create(merry=merry, slot_no=slot_no, defaults={"weekday": weekday})
            if obj.weekday != weekday:
                obj.weekday = weekday
                obj.full_clean()
                obj.save(update_fields=["weekday"])

        return Response({"message": "Slot config saved."}, status=status.HTTP_200_OK)


# ==========================================
# Join requests flow (UPDATED for seats)
# ==========================================
class RequestToJoinMerryView(APIView):
    """
    POST /api/merry/<merry_id>/join/request/
    body: { note?, requested_seats? }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)

        if MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists():
            raise ValidationError("You are already a member of this merry.")

        note = (request.data.get("note") or "").strip()[:255]
        requested_seats = request.data.get("requested_seats") or 1
        try:
            requested_seats = int(requested_seats)
        except Exception:
            raise ValidationError("requested_seats must be an integer.")
        if requested_seats < 1 or requested_seats > 50:
            raise ValidationError("requested_seats must be between 1 and 50.")

        existing = MerryJoinRequest.objects.filter(merry=merry, user=request.user).first()
        if existing:
            if existing.status == "PENDING":
                return Response(
                    {
                        "message": "Join request already pending.",
                        "request_id": existing.id,
                        "status": existing.status,
                        "requested_seats": existing.requested_seats,
                    },
                    status=status.HTTP_200_OK,
                )

            existing.status = "PENDING"
            existing.note = note
            existing.requested_seats = requested_seats
            existing.reviewed_by = None
            existing.reviewed_at = None
            existing.created_at = timezone.now()
            existing.full_clean()
            existing.save(update_fields=["status", "note", "requested_seats", "reviewed_by", "reviewed_at", "created_at"])

            return Response(
                {
                    "message": "Join request re-submitted.",
                    "request_id": existing.id,
                    "status": existing.status,
                    "requested_seats": existing.requested_seats,
                },
                status=status.HTTP_201_CREATED,
            )

        jr = MerryJoinRequest(merry=merry, user=request.user, status="PENDING", note=note, requested_seats=requested_seats)
        jr.full_clean()
        jr.save()

        return Response(
            {"message": "Join request submitted.", "request_id": jr.id, "status": jr.status, "requested_seats": jr.requested_seats},
            status=status.HTTP_201_CREATED,
        )


class CancelJoinRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, request_id: int):
        jr = MerryJoinRequest.objects.filter(id=request_id, user=request.user).first()
        if not jr:
            raise ValidationError("Join request not found.")
        jr.cancel(request.user)
        return Response({"message": "Join request cancelled."}, status=status.HTTP_200_OK)


class MyJoinRequestsView(APIView):
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
                "requested_seats": r.requested_seats,
                "created_at": r.created_at,
                "reviewed_at": r.reviewed_at,
            }
            for r in qs
        ]
        return Response(data, status=status.HTTP_200_OK)


class AdminListJoinRequestsView(APIView):
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
                "requested_seats": r.requested_seats,
                "created_at": r.created_at,
            }
            for r in qs
        ]
        return Response(data, status=status.HTTP_200_OK)


class AdminApproveJoinRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, request_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        jr = (
            MerryJoinRequest.objects.select_for_update()
            .select_related("merry", "user")
            .filter(id=request_id)
            .first()
        )
        if not jr:
            raise ValidationError("Join request not found.")

        member, seats = jr.approve(request.user)

        return Response(
            {
                "message": "Join request approved.",
                "member_id": member.id,
                "merry_id": member.merry_id,
                "user_id": member.user_id,
                "seats_created": [
                    {"seat_id": s.id, "seat_no": s.seat_no, "payout_position": s.payout_position}
                    for s in seats
                ],
            },
            status=status.HTTP_200_OK,
        )


class AdminRejectJoinRequestView(APIView):
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


# ==========================================
# Dues & Payments (replaces old MerryContribution)
# ==========================================
class EnsureDuesForCurrentPeriodView(APIView):
    """
    POST /api/merry/<merry_id>/dues/ensure/
    Admin generates dues rows for all active seats for the current (or specified) period.
    body: { period_key? }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, merry_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")
        merry = get_merry_or_404(merry_id)

        period_key = (request.data.get("period_key") or "").strip() or current_period_key(merry)
        created = merry.ensure_dues_for_period(period_key=period_key)

        return Response(
            {"message": "Dues ensured.", "period_key": period_key, "created": created},
            status=status.HTTP_200_OK,
        )


class MyMerryDuesView(APIView):
    """
    GET /api/merry/<merry_id>/dues/my/?period_key=...
    Shows dues per slot across all seats for the logged-in member.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        member = get_member_or_404(merry_id, request.user)
        merry = member.merry

        period_key = (request.query_params.get("period_key") or "").strip() or current_period_key(merry)

        # Make sure dues exist (optional auto-ensure for user view)
        merry.ensure_dues_for_period(period_key=period_key)

        dues = (
            MerryContributionDue.objects.filter(merry=merry, seat__member=member, period_key=period_key, seat__is_active=True)
            .select_related("seat")
            .order_by("slot_no", "seat__seat_no", "id")
        )

        data = [
            {
                "due_id": d.id,
                "period_key": d.period_key,
                "slot_no": d.slot_no,
                "seat_id": d.seat_id,
                "seat_no": d.seat.seat_no,
                "due_amount": str(d.due_amount),
                "paid_amount": str(d.paid_amount),
                "status": d.status,
                "outstanding": str(d.outstanding()),
                "updated_at": d.updated_at,
            }
            for d in dues
        ]

        return Response(
            {
                "merry_id": merry.id,
                "period_key": period_key,
                "payouts_per_period": merry.payouts_per_period,
                "data": data,
            },
            status=status.HTTP_200_OK,
        )


class AdminDuesView(APIView):
    """
    GET /api/merry/<merry_id>/dues/?period_key=...&slot_no=...
    Admin view of dues for reporting & outstanding.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")
        merry = get_merry_or_404(merry_id)

        period_key = (request.query_params.get("period_key") or "").strip() or current_period_key(merry)
        slot_no = request.query_params.get("slot_no")

        # ensure exists for admin reporting too
        merry.ensure_dues_for_period(period_key=period_key)

        qs = MerryContributionDue.objects.filter(merry=merry, period_key=period_key).select_related(
            "seat", "seat__member", "seat__member__user"
        )

        if slot_no is not None and str(slot_no).strip() != "":
            try:
                slot_no = int(slot_no)
            except Exception:
                raise ValidationError("slot_no must be an integer.")
            validate_slot(merry, slot_no)
            qs = qs.filter(slot_no=slot_no)

        qs = qs.order_by("slot_no", "seat__member__user_id", "seat__seat_no", "id")

        data = []
        for d in qs:
            u = d.seat.member.user
            data.append(
                {
                    "due_id": d.id,
                    "period_key": d.period_key,
                    "slot_no": d.slot_no,
                    "seat_id": d.seat_id,
                    "seat_no": d.seat.seat_no,
                    "member_id": d.seat.member_id,
                    "user_id": u.id,
                    "username": getattr(u, "username", None),
                    "phone": getattr(u, "phone", None),
                    "due_amount": str(d.due_amount),
                    "paid_amount": str(d.paid_amount),
                    "status": d.status,
                    "outstanding": str(d.outstanding()),
                    "updated_at": d.updated_at,
                }
            )

        total_due = qs.aggregate(s=Sum("due_amount")).get("s") or Decimal("0")
        total_paid = qs.aggregate(s=Sum("paid_amount")).get("s") or Decimal("0")

        return Response(
            {
                "merry_id": merry.id,
                "period_key": period_key,
                "slot_no": slot_no,
                "total_due": str(q2(total_due)),
                "total_paid_allocated": str(q2(total_paid)),
                "rows": data,
            },
            status=status.HTTP_200_OK,
        )


class CreatePaymentIntentView(APIView):
    """
    POST /api/merry/<merry_id>/payments/intent/
    Member creates a payment intent (STK will be handled by payments app):
      body: { amount, payer_phone? }
    - beneficiary is ALWAYS the logged-in member (as you requested)
    - payer_phone is the STK phone that will be charged (can be different)
    - allocation happens when payment becomes CONFIRMED (via callback endpoint / admin mark)
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, merry_id: int):
        member = get_member_or_404(merry_id, request.user)
        merry = member.merry

        amount_in = request.data.get("amount")
        if amount_in is None:
            raise ValidationError("amount is required.")
        amount = q2(Decimal(str(amount_in)))
        if amount <= 0:
            raise ValidationError("amount must be > 0.")

        payer_phone = (request.data.get("payer_phone") or getattr(request.user, "phone", "") or "").strip()
        if not payer_phone:
            raise ValidationError("payer_phone is required (or user must have a phone).")

        period_key = current_period_key(merry)

        # Ensure dues exist at least for current period (so allocation has something to target)
        merry.ensure_dues_for_period(period_key=period_key)

        pay = MerryPayment.objects.create(
            merry=merry,
            beneficiary_member=member,
            initiated_by=request.user,
            payer_phone=payer_phone,
            period_key=period_key,
            amount=amount,
            status="PENDING",
        )

        # NOTE:
        # Here you would call your Payments app STK push with:
        #  - phone = payer_phone
        #  - amount = amount
        #  - reference = pay.id (or a UUID)
        # and later callback would mark pay CONFIRMED.
        #
        # This view only creates the intent record.

        return Response(
            {
                "message": "Payment intent created.",
                "payment_id": pay.id,
                "merry_id": merry.id,
                "beneficiary_member_id": member.id,
                "amount": str(pay.amount),
                "payer_phone": pay.payer_phone,
                "period_key": pay.period_key,
                "status": pay.status,
            },
            status=status.HTTP_201_CREATED,
        )


class MyPaymentsView(APIView):
    """
    GET /api/merry/payments/my/
    Shows last payments for logged-in user across all their merries.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = (
            MerryPayment.objects.filter(beneficiary_member__user=request.user)
            .select_related("merry", "beneficiary_member", "beneficiary_member__user")
            .order_by("-created_at")[:200]
        )

        data = [
            {
                "id": p.id,
                "merry_id": p.merry_id,
                "merry_name": p.merry.name,
                "beneficiary_member_id": p.beneficiary_member_id,
                "amount": str(p.amount),
                "status": p.status,
                "paid_at": p.paid_at,
                "payer_phone": p.payer_phone,
                "mpesa_receipt_number": p.mpesa_receipt_number,
                "period_key": p.period_key,
                "created_at": p.created_at,
            }
            for p in qs
        ]
        return Response(data, status=status.HTTP_200_OK)


class AdminMarkPaymentConfirmedView(APIView):
    """
    POST /api/merry/payments/<payment_id>/confirm/
    Admin-only helper (useful in dev/testing).
    In production, your Mpesa callback should do the CONFIRMED update.
    After confirming, we allocate the payment into dues automatically.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, payment_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        receipt = (request.data.get("mpesa_receipt_number") or "").strip()[:64]

        p = (
            MerryPayment.objects.select_for_update()
            .select_related("merry", "beneficiary_member")
            .filter(id=payment_id)
            .first()
        )
        if not p:
            raise ValidationError("Payment not found.")
        if p.status == "CONFIRMED":
            return Response({"message": "Already CONFIRMED."}, status=status.HTTP_200_OK)

        p.status = "CONFIRMED"
        p.paid_at = timezone.now()
        if receipt:
            p.mpesa_receipt_number = receipt
        p.full_clean()
        p.save(update_fields=["status", "paid_at", "mpesa_receipt_number"])

        # Allocate into dues (slot-first, seat-aware)
        allocate_payment(p.id)

        return Response({"message": "Payment confirmed and allocated."}, status=status.HTTP_200_OK)


# ==========================================
# Payout schedule + records (seat-based)
# ==========================================
class MerryPayoutScheduleView(APIView):
    """
    GET /api/merry/<merry_id>/payouts/schedule/
    Shows current period + used slots + seats list.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        is_member = MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists()
        if not is_admin(request.user) and not is_member:
            raise PermissionDenied("Not allowed.")

        period_key = current_period_key(merry)

        used_slots = list(
            MerryPayout.objects.filter(merry=merry, period_key=period_key)
            .order_by("slot_no")
            .values_list("slot_no", flat=True)
        )

        seats_qs = MerrySeat.objects.filter(merry=merry, is_active=True).select_related("member", "member__user")
        if merry.payout_order_type == "manual":
            seats_qs = seats_qs.order_by("payout_position", "id")
        else:
            seats_qs = seats_qs.order_by("id")

        seats = [
            {
                "seat_id": s.id,
                "member_id": s.member_id,
                "user_id": s.member.user_id,
                "username": getattr(s.member.user, "username", None),
                "phone": getattr(s.member.user, "phone", None),
                "seat_no": s.seat_no,
                "payout_position": s.payout_position,
            }
            for s in seats_qs
        ]

        return Response(
            {
                "merry": {
                    "id": merry.id,
                    "name": merry.name,
                    "payout_order_type": merry.payout_order_type,
                    "contribution_amount": str(merry.contribution_amount),
                    "members_count": merry.members.filter(is_active=True).count(),
                    "seats_count": merry.seats.filter(is_active=True).count(),
                    "payout_frequency": merry.payout_frequency,
                    "payouts_per_period": payouts_per_period(merry),
                },
                "current_period_key": period_key,
                "used_slots_in_period": used_slots,
                "seats": seats,
            },
            status=status.HTTP_200_OK,
        )


class CreatePayoutView(APIView):
    """
    POST /api/merry/<merry_id>/payouts/create/
    body:
      - seat_id (required)
      - period_key (optional, default current)
      - slot_no (optional, default next available)
      - amount (optional, default computed as total collected per slot or custom)
      - compute_amount (optional bool):
          if true => amount = total paid allocations for this (period_key, slot_no)
      - notes (optional)

    NOTE: payout is seat-based now (fair for multi-seat members).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, merry_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        merry = get_merry_or_404(merry_id)

        seat_id = request.data.get("seat_id")
        if not seat_id:
            raise ValidationError("seat_id is required.")

        seat = MerrySeat.objects.filter(id=seat_id, merry=merry, is_active=True).select_related("member").first()
        if not seat:
            raise ValidationError("Seat not found in this merry.")

        period_key = (request.data.get("period_key") or "").strip() or current_period_key(merry)

        slot_no = request.data.get("slot_no")
        if slot_no is None:
            slot_no = next_available_slot(merry, period_key)
        else:
            try:
                slot_no = int(slot_no)
            except Exception:
                raise ValidationError("slot_no must be an integer.")
            validate_slot(merry, slot_no)

        if MerryPayout.objects.filter(merry=merry, period_key=period_key, slot_no=slot_no).exists():
            raise ValidationError(f"A payout already exists for period {period_key} slot {slot_no}.")

        if MerryPayout.objects.filter(merry=merry, seat=seat, period_key=period_key).exists():
            raise ValidationError("This seat already has a payout in this period.")

        compute_amount = bool(request.data.get("compute_amount") or False)
        notes = (request.data.get("notes") or "")[:255]
        amount_in = request.data.get("amount")

        if compute_amount:
            # Compute amount as the total actually PAID into dues for that slot in that period
            # (sum of paid_amount on dues for that slot/period across all seats).
            merry.ensure_dues_for_period(period_key=period_key)

            total_paid_for_slot = (
                MerryContributionDue.objects.filter(merry=merry, period_key=period_key, slot_no=slot_no)
                .aggregate(s=Sum("paid_amount"))
                .get("s")
                or Decimal("0")
            )
            total_paid_for_slot = q2(total_paid_for_slot)
            if total_paid_for_slot <= 0:
                raise ValidationError("No funds allocated for this slot yet. Cannot compute payout amount.")
            amount = total_paid_for_slot
        else:
            if amount_in is None:
                raise ValidationError("amount is required (or set compute_amount=true).")
            amount = q2(Decimal(str(amount_in)))
            if amount <= 0:
                raise ValidationError("amount must be > 0.")

        payout = MerryPayout.objects.create(
            merry=merry,
            seat=seat,
            period_key=period_key,
            slot_no=slot_no,
            amount=amount,
            status="SCHEDULED",
            notes=notes,
        )

        return Response(
            {
                "message": "Payout record created.",
                "payout_id": payout.id,
                "status": payout.status,
                "merry_id": merry.id,
                "seat_id": seat.id,
                "member_id": seat.member_id,
                "user_id": seat.member.user_id,
                "amount": str(payout.amount),
                "period_key": payout.period_key,
                "slot_no": payout.slot_no,
            },
            status=status.HTTP_201_CREATED,
        )


class MarkPayoutPaidView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, payout_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        p = MerryPayout.objects.select_related("merry", "seat", "seat__member").filter(id=payout_id).first()
        if not p:
            raise ValidationError("Payout not found.")
        if p.status == "PAID":
            return Response({"message": "Already PAID."}, status=status.HTTP_200_OK)

        p.status = "PAID"
        p.paid_at = timezone.now()
        p.save(update_fields=["status", "paid_at"])

        return Response({"message": "Payout marked PAID."}, status=status.HTTP_200_OK)