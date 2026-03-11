# merry/views.py
# FULLY UPDATED — Admin-approval join flow + seats/shares + slot-based dues
# + payments + allocations + seat-based payouts
# + safer parsing/validation + better locking + duplicate receipt protection

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

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


def parse_decimal(value, field_name: str) -> Decimal:
    if value is None or value == "":
        raise ValidationError(f"{field_name} is required.")
    try:
        return q2(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(f"{field_name} must be a valid number.")


def parse_int(value, field_name: str, *, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    try:
        n = int(value)
    except (ValueError, TypeError):
        raise ValidationError(f"{field_name} must be an integer.")

    if min_value is not None and n < min_value:
        raise ValidationError(f"{field_name} must be >= {min_value}.")
    if max_value is not None and n > max_value:
        raise ValidationError(f"{field_name} must be <= {max_value}.")
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


def get_merry_or_404(merry_id: int) -> MerryGoRound:
    merry = MerryGoRound.objects.filter(id=merry_id).select_related("created_by").first()
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
        MerryPayout.objects.filter(merry=merry, period_key=period_key).values_list("slot_no", flat=True)
    )
    for s in range(1, limit + 1):
        if s not in used:
            return s
    raise ValidationError(f"Payout slots are full for period {period_key}. Max slots: {limit}.")


def user_can_view_merry(user, merry: MerryGoRound) -> bool:
    if is_admin(user):
        return True
    return MerryMember.objects.filter(merry=merry, user=user, is_active=True).exists()


# ==========================================
# Allocation engine (slot-first, seat-aware)
# ==========================================
def _next_week_period_key(period_key: str) -> str:
    try:
        year = int(period_key[:4])
        week = int(period_key.split("-W")[1])
    except Exception:
        raise ValidationError("Invalid WEEKLY period_key format. Expected YYYY-W##.")

    from datetime import date, timedelta

    d = date.fromisocalendar(year, week, 1) + timedelta(days=7)
    y, w, _ = d.isocalendar()
    return f"{y:04d}-W{w:02d}"


def _next_month_period_key(period_key: str) -> str:
    try:
        year = int(period_key[:4])
        month = int(period_key.split("-")[1])
    except Exception:
        raise ValidationError("Invalid MONTHLY period_key format. Expected YYYY-MM.")

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
        for slot_no in range(1, payouts_per_period(merry) + 1):
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

    if not member.is_active:
        raise ValidationError("Cannot allocate payment for an inactive member.")

    remaining = payment.amount or Decimal("0")
    if remaining <= 0:
        raise ValidationError("Payment amount must be > 0.")

    period_key = payment.period_key
    safety = 0

    while remaining > 0:
        safety += 1
        if safety > 2000:
            raise ValidationError("Allocation safety limit reached (period loop).")

        _ensure_dues_for_member_period(merry, member, period_key)

        dues = list(
            MerryContributionDue.objects.select_for_update()
            .filter(
                merry=merry,
                seat__member=member,
                period_key=period_key,
                seat__is_active=True,
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


# ==========================================
# Merry list / create / detail
# ==========================================
class AvailableMerriesView(APIView):
    """
    GET /api/merry/available/
    Shows merries user has not yet joined, including join/open info.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        member_merry_ids = list(
            MerryMember.objects.filter(user=request.user, is_active=True).values_list("merry_id", flat=True)
        )

        latest_join_requests = {}
        for r in MerryJoinRequest.objects.filter(user=request.user).order_by("-created_at", "-id"):
            if r.merry_id not in latest_join_requests:
                latest_join_requests[r.merry_id] = r

        qs = MerryGoRound.objects.exclude(id__in=member_merry_ids).order_by("-id")

        data = []
        for m in qs:
            jr = latest_join_requests.get(m.id)
            data.append(
                {
                    "id": m.id,
                    "name": m.name,
                    "contribution_amount": str(m.contribution_amount),
                    "cycle_duration_weeks": m.cycle_duration_weeks,
                    "payout_order_type": m.payout_order_type,
                    "next_payout_date": m.next_payout_date,
                    "payout_frequency": m.payout_frequency,
                    "payouts_per_period": m.payouts_per_period,
                    "is_open": getattr(m, "is_open", True),
                    "max_seats": getattr(m, "max_seats", 0),
                    "available_seats": m.available_seats() if hasattr(m, "available_seats") else None,
                    "members_count": m.members.filter(is_active=True).count(),
                    "seats_count": m.seats.filter(is_active=True).count(),
                    "my_join_request": (
                        {
                            "id": jr.id,
                            "status": jr.status,
                            "requested_seats": jr.requested_seats,
                            "created_at": jr.created_at,
                            "reviewed_at": jr.reviewed_at,
                        }
                        if jr
                        else None
                    ),
                    "created_at": m.created_at,
                }
            )

        return Response(data, status=status.HTTP_200_OK)


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
                "is_open": getattr(m, "is_open", True),
                "max_seats": getattr(m, "max_seats", 0),
                "available_seats": m.available_seats() if hasattr(m, "available_seats") else None,
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
                    "is_open": getattr(mm.merry, "is_open", True),
                    "max_seats": getattr(mm.merry, "max_seats", 0),
                    "available_seats": mm.merry.available_seats() if hasattr(mm.merry, "available_seats") else None,
                    "joined_at": mm.joined_at,
                    "seats_count": seats_count,
                }
            )

        return Response({"created": created_data, "memberships": member_data}, status=status.HTTP_200_OK)


class CreateMerryView(APIView):
    """
    POST /api/merry/create/
    Admin creates a merry.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        name = (request.data.get("name") or "").strip()
        if not name:
            raise ValidationError("name is required.")

        amount = parse_decimal(request.data.get("contribution_amount"), "contribution_amount")
        if amount <= 0:
            raise ValidationError("contribution_amount must be > 0.")

        cycle_duration_weeks = parse_int(
            request.data.get("cycle_duration_weeks", 1),
            "cycle_duration_weeks",
            min_value=1,
        )

        payout_order_type = (request.data.get("payout_order_type") or "manual").strip().lower()
        if payout_order_type not in ("manual", "random"):
            raise ValidationError("payout_order_type must be 'manual' or 'random'.")

        payout_frequency = (request.data.get("payout_frequency") or "WEEKLY").strip().upper()
        if payout_frequency not in ("WEEKLY", "MONTHLY"):
            raise ValidationError("payout_frequency must be 'WEEKLY' or 'MONTHLY'.")

        payouts_pp = parse_int(
            request.data.get("payouts_per_period", 1),
            "payouts_per_period",
            min_value=1,
            max_value=14,
        )

        is_open = parse_bool(request.data.get("is_open"), default=True)
        max_seats = parse_int(request.data.get("max_seats", 0), "max_seats", min_value=0)
        next_payout_date = request.data.get("next_payout_date") or None

        merry = MerryGoRound.objects.create(
            name=name,
            contribution_amount=amount,
            cycle_duration_weeks=cycle_duration_weeks,
            payout_order_type=payout_order_type,
            next_payout_date=next_payout_date,
            created_by=request.user,
            payout_frequency=payout_frequency,
            payouts_per_period=payouts_pp,
            is_open=is_open,
            max_seats=max_seats,
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
                "is_open": merry.is_open,
                "max_seats": merry.max_seats,
                "available_seats": merry.available_seats() if hasattr(merry, "available_seats") else None,
                "created_at": merry.created_at,
            },
            status=status.HTTP_201_CREATED,
        )


class MerryDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)

        if not user_can_view_merry(request.user, merry):
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
                "is_open": getattr(merry, "is_open", True),
                "max_seats": getattr(merry, "max_seats", 0),
                "available_seats": merry.available_seats() if hasattr(merry, "available_seats") else None,
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
        if not user_can_view_merry(request.user, merry):
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
        if not user_can_view_merry(request.user, merry):
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
        if not user_can_view_merry(request.user, merry):
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

        seen = set()
        for it in items:
            slot_no = parse_int(it.get("slot_no"), "slot_no", min_value=1)
            weekday = parse_int(it.get("weekday"), "weekday", min_value=0, max_value=6)
            validate_slot(merry, slot_no)

            if slot_no in seen:
                raise ValidationError("Duplicate slot_no in payload.")
            seen.add(slot_no)

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

    @transaction.atomic
    def post(self, request, merry_id: int):
        merry = MerryGoRound.objects.select_for_update().filter(id=merry_id).first()
        if not merry:
            raise ValidationError("Merry not found.")

        if MerryMember.objects.filter(merry=merry, user=request.user, is_active=True).exists():
            raise ValidationError("You are already a member of this merry.")

        note = (request.data.get("note") or "").strip()[:255]
        requested_seats = parse_int(
            request.data.get("requested_seats", 1),
            "requested_seats",
            min_value=1,
            max_value=50,
        )

        if hasattr(merry, "can_accept_join_request"):
            ok, reason = merry.can_accept_join_request(requested_seats)
            if not ok:
                raise ValidationError(reason)

        existing_pending = (
            MerryJoinRequest.objects.select_for_update()
            .filter(merry=merry, user=request.user, status="PENDING")
            .first()
        )
        if existing_pending:
            return Response(
                {
                    "message": "Join request already pending.",
                    "request_id": existing_pending.id,
                    "status": existing_pending.status,
                    "requested_seats": existing_pending.requested_seats,
                },
                status=status.HTTP_200_OK,
            )

        existing_latest = (
            MerryJoinRequest.objects.select_for_update()
            .filter(merry=merry, user=request.user)
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

            return Response(
                {
                    "message": "Join request re-submitted.",
                    "request_id": existing_latest.id,
                    "status": existing_latest.status,
                    "requested_seats": existing_latest.requested_seats,
                },
                status=status.HTTP_201_CREATED,
            )

        jr = MerryJoinRequest(
            merry=merry,
            user=request.user,
            status="PENDING",
            note=note,
            requested_seats=requested_seats,
        )
        jr.full_clean()
        jr.save()

        return Response(
            {
                "message": "Join request submitted.",
                "request_id": jr.id,
                "status": jr.status,
                "requested_seats": jr.requested_seats,
            },
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
        qs = MerryJoinRequest.objects.filter(user=request.user).select_related("merry").order_by("-created_at", "-id")
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

        status_filter = (request.query_params.get("status") or "").strip().upper()
        qs = MerryJoinRequest.objects.filter(merry=merry).select_related("user").order_by("-created_at", "-id")
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
                "reviewed_at": r.reviewed_at,
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

        MerryGoRound.objects.select_for_update().filter(id=jr.merry_id).first()

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
        merry.ensure_dues_for_period(period_key=period_key)

        dues = (
            MerryContributionDue.objects.filter(
                merry=merry,
                seat__member=member,
                period_key=period_key,
                seat__is_active=True,
            )
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

        merry.ensure_dues_for_period(period_key=period_key)

        qs = MerryContributionDue.objects.filter(merry=merry, period_key=period_key).select_related(
            "seat", "seat__member", "seat__member__user"
        )

        parsed_slot_no = None
        if slot_no is not None and str(slot_no).strip() != "":
            parsed_slot_no = parse_int(slot_no, "slot_no", min_value=1)
            validate_slot(merry, parsed_slot_no)
            qs = qs.filter(slot_no=parsed_slot_no)

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

        totals = qs.aggregate(total_due=Sum("due_amount"), total_paid=Sum("paid_amount"))

        return Response(
            {
                "merry_id": merry.id,
                "period_key": period_key,
                "slot_no": parsed_slot_no,
                "total_due": str(q2(totals.get("total_due") or Decimal("0"))),
                "total_paid_allocated": str(q2(totals.get("total_paid") or Decimal("0"))),
                "rows": data,
            },
            status=status.HTTP_200_OK,
        )


class CreatePaymentIntentView(APIView):
    """
    POST /api/merry/<merry_id>/payments/intent/
    Member creates a payment intent (STK will be handled by payments app):
      body: { amount, payer_phone? }
    - beneficiary is ALWAYS the logged-in member
    - payer_phone is the STK phone that will be charged
    - allocation happens when payment becomes CONFIRMED
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, merry_id: int):
        member = get_member_or_404(merry_id, request.user)
        merry = member.merry

        amount = parse_decimal(request.data.get("amount"), "amount")
        if amount <= 0:
            raise ValidationError("amount must be > 0.")

        payer_phone = (request.data.get("payer_phone") or getattr(request.user, "phone", "") or "").strip()
        if not payer_phone:
            raise ValidationError("payer_phone is required (or user must have a phone).")

        period_key = current_period_key(merry)
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
    Admin-only helper.
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

        if receipt:
            exists_elsewhere = MerryPayment.objects.exclude(id=p.id).filter(mpesa_receipt_number=receipt).exists()
            if exists_elsewhere:
                raise ValidationError("This M-Pesa receipt number is already used.")

        if p.status == "CONFIRMED":
            return Response({"message": "Already CONFIRMED."}, status=status.HTTP_200_OK)

        if p.status in ("FAILED", "CANCELLED"):
            raise ValidationError(f"Cannot confirm a {p.status} payment.")

        p.status = "CONFIRMED"
        p.paid_at = timezone.now()
        if receipt:
            p.mpesa_receipt_number = receipt
            p.full_clean()
            p.save(update_fields=["status", "paid_at", "mpesa_receipt_number"])
        else:
            p.full_clean()
            p.save(update_fields=["status", "paid_at"])

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
        if not user_can_view_merry(request.user, merry):
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
                    "is_open": getattr(merry, "is_open", True),
                    "max_seats": getattr(merry, "max_seats", 0),
                    "available_seats": merry.available_seats() if hasattr(merry, "available_seats") else None,
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

    @transaction.atomic
    def post(self, request, merry_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        merry = MerryGoRound.objects.select_for_update().filter(id=merry_id).first()
        if not merry:
            raise ValidationError("Merry not found.")

        seat_id = request.data.get("seat_id")
        if not seat_id:
            raise ValidationError("seat_id is required.")

        seat = (
            MerrySeat.objects.select_for_update()
            .select_related("member", "member__user")
            .filter(id=seat_id, merry=merry, is_active=True)
            .first()
        )
        if not seat:
            raise ValidationError("Seat not found in this merry.")

        period_key = (request.data.get("period_key") or "").strip() or current_period_key(merry)

        slot_no_raw = request.data.get("slot_no")
        if slot_no_raw is None or str(slot_no_raw).strip() == "":
            slot_no = next_available_slot(merry, period_key)
        else:
            slot_no = parse_int(slot_no_raw, "slot_no", min_value=1)
            validate_slot(merry, slot_no)

        if MerryPayout.objects.filter(merry=merry, period_key=period_key, slot_no=slot_no).exists():
            raise ValidationError(f"A payout already exists for period {period_key} slot {slot_no}.")

        if MerryPayout.objects.filter(merry=merry, seat=seat, period_key=period_key).exists():
            raise ValidationError("This seat already has a payout in this period.")

        compute_amount = parse_bool(request.data.get("compute_amount"), default=False)
        notes = (request.data.get("notes") or "")[:255]

        if compute_amount:
            merry.ensure_dues_for_period(period_key=period_key)

            total_paid_for_slot = (
                MerryContributionDue.objects.filter(merry=merry, period_key=period_key, slot_no=slot_no)
                .aggregate(s=Sum("paid_amount"))
                .get("s")
                or Decimal("0")
            )
            amount = q2(total_paid_for_slot)
            if amount <= 0:
                raise ValidationError("No funds allocated for this slot yet. Cannot compute payout amount.")
        else:
            amount = parse_decimal(request.data.get("amount"), "amount")
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

        if p.status in ("FAILED", "CANCELLED"):
            raise ValidationError(f"Cannot mark a {p.status} payout as PAID.")

        p.status = "PAID"
        p.paid_at = timezone.now()
        p.save(update_fields=["status", "paid_at"])

        return Response({"message": "Payout marked PAID."}, status=status.HTTP_200_OK)