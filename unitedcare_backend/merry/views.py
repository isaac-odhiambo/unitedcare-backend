# merry/views.py
# ROSCA compatibility version
# ---------------------------------------------------------
# - Keeps most endpoints and response shapes stable
# - Aligns flow with queue-based ROSCA (one current payout at a time)
# - Preserves wallet, payments, join requests, dashboard, payouts
# - Keeps some legacy compatibility endpoints but narrows them to slot_no=1
# - Exposes penalty policy and turn-linked payout/due fields
# ---------------------------------------------------------

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
    MerryWallet,
    MerryWalletTransaction,
)
from . import services as merry_services


# ==========================================
# Helpers
# ==========================================
def q2(x: Decimal) -> Decimal:
    return Decimal(str(x or "0")).quantize(Decimal("0.01"))


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
    if hasattr(merry, "effective_payouts_per_period"):
        return max(1, int(merry.effective_payouts_per_period() or 1))
    return 1


def validate_slot(merry: MerryGoRound, slot_no: int) -> None:
    limit = payouts_per_period(merry)
    if slot_no < 1 or slot_no > limit:
        raise ValidationError(f"slot_no must be between 1 and {limit}.")


def next_available_slot(merry: MerryGoRound, period_key: str) -> int:
    return 1


def user_can_view_merry(user, merry: MerryGoRound) -> bool:
    if is_admin(user):
        return True
    return MerryMember.objects.filter(merry=merry, user=user, is_active=True).exists()


def _service_error(e: Exception) -> ValidationError:
    return ValidationError(str(e))


def _merry_policy_dict(merry: MerryGoRound) -> dict:
    return {
        "penalty_mode": getattr(merry, "penalty_mode", "NONE"),
        "flat_penalty_amount": str(q2(getattr(merry, "flat_penalty_amount", Decimal("0.00")) or Decimal("0.00"))),
        "daily_penalty_amount": str(q2(getattr(merry, "daily_penalty_amount", Decimal("0.00")) or Decimal("0.00"))),
        "penalty_grace_days": int(getattr(merry, "penalty_grace_days", 0) or 0),
        "penalty_cap_amount": (
            str(q2(getattr(merry, "penalty_cap_amount")))
            if getattr(merry, "penalty_cap_amount", None) is not None
            else None
        ),
    }


# ==========================================
# Allocation engine (backward-compatible wrapper)
# ==========================================
@transaction.atomic
def _ensure_dues_for_member_period(merry: MerryGoRound, member: MerryMember, period_key: str) -> None:
    try:
        merry_services.ensure_dues_for_member_period(merry, member, period_key)
    except Exception as e:
        raise ValidationError(str(e))


@transaction.atomic
def allocate_payment(payment_id: int) -> MerryPayment:
    try:
        return merry_services.allocate_payment(payment_id=payment_id)
    except Exception as e:
        raise ValidationError(str(e))


# ==========================================
# Merry list / create / detail
# ==========================================
class AvailableMerriesView(APIView):
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
            available_seats = m.available_seats() if hasattr(m, "available_seats") else None
            available_seat_numbers = (
                m.available_seat_numbers() if hasattr(m, "available_seat_numbers") else None
            )

            row = {
                "id": m.id,
                "name": m.name,
                "contribution_amount": str(m.contribution_amount),
                "cycle_duration_weeks": m.cycle_duration_weeks,
                "payout_order_type": m.payout_order_type,
                "next_payout_date": m.next_payout_date,
                "payout_frequency": m.payout_frequency,
                "payouts_per_period": 1,
                "is_open": getattr(m, "is_open", True),
                "max_seats": getattr(m, "max_seats", 0),
                "available_seats": available_seats,
                "available_seat_numbers": available_seat_numbers,
                "members_count": m.members.filter(is_active=True).count(),
                "seats_count": m.seats.filter(is_active=True).count(),
                "can_request_join": bool(
                    getattr(m, "is_open", True) and (available_seats is None or available_seats > 0)
                ),
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
            row.update(_merry_policy_dict(m))
            data.append(row)

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

        created_data = []
        for m in created:
            row = {
                "id": m.id,
                "name": m.name,
                "contribution_amount": str(m.contribution_amount),
                "cycle_duration_weeks": m.cycle_duration_weeks,
                "payout_order_type": m.payout_order_type,
                "next_payout_date": m.next_payout_date,
                "payout_frequency": m.payout_frequency,
                "payouts_per_period": 1,
                "is_open": getattr(m, "is_open", True),
                "max_seats": getattr(m, "max_seats", 0),
                "available_seats": m.available_seats() if hasattr(m, "available_seats") else None,
                "members_count": m.members.filter(is_active=True).count(),
                "seats_count": m.seats.filter(is_active=True).count(),
                "created_at": m.created_at,
            }
            row.update(_merry_policy_dict(m))
            created_data.append(row)

        member_data = []
        for mm in memberships:
            seats_count = mm.seats.filter(is_active=True).count()
            row = {
                "merry_id": mm.merry_id,
                "name": mm.merry.name,
                "contribution_amount": str(mm.merry.contribution_amount),
                "cycle_duration_weeks": mm.merry.cycle_duration_weeks,
                "payout_order_type": mm.merry.payout_order_type,
                "next_payout_date": mm.merry.next_payout_date,
                "payout_frequency": mm.merry.payout_frequency,
                "payouts_per_period": 1,
                "is_open": getattr(mm.merry, "is_open", True),
                "max_seats": getattr(mm.merry, "max_seats", 0),
                "available_seats": mm.merry.available_seats() if hasattr(mm.merry, "available_seats") else None,
                "joined_at": mm.joined_at,
                "seats_count": seats_count,
            }
            row.update(_merry_policy_dict(mm.merry))
            member_data.append(row)

        return Response({"created": created_data, "memberships": member_data}, status=status.HTTP_200_OK)


class CreateMerryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        try:
            merry = merry_services.create_merry(
                creator=request.user,
                name=(request.data.get("name") or "").strip(),
                contribution_amount=request.data.get("contribution_amount"),
                cycle_duration_weeks=request.data.get("cycle_duration_weeks", 1),
                payout_order_type=request.data.get("payout_order_type", "manual"),
                next_payout_date=request.data.get("next_payout_date") or None,
                payout_frequency=request.data.get("payout_frequency", "WEEKLY"),
                payouts_per_period=1,
                is_open=request.data.get("is_open"),
                max_seats=request.data.get("max_seats", 0),
                penalty_mode=request.data.get("penalty_mode", "NONE"),
                flat_penalty_amount=request.data.get("flat_penalty_amount", "0.00"),
                daily_penalty_amount=request.data.get("daily_penalty_amount", "0.00"),
                penalty_grace_days=request.data.get("penalty_grace_days", 0),
                penalty_cap_amount=request.data.get("penalty_cap_amount", None),
            )
        except Exception as e:
            raise _service_error(e)

        data = {
            "id": merry.id,
            "name": merry.name,
            "contribution_amount": str(merry.contribution_amount),
            "cycle_duration_weeks": merry.cycle_duration_weeks,
            "payout_order_type": merry.payout_order_type,
            "next_payout_date": merry.next_payout_date,
            "payout_frequency": merry.payout_frequency,
            "payouts_per_period": 1,
            "is_open": merry.is_open,
            "max_seats": merry.max_seats,
            "available_seats": merry.available_seats() if hasattr(merry, "available_seats") else None,
            "created_at": merry.created_at,
        }
        data.update(_merry_policy_dict(merry))

        return Response(data, status=status.HTTP_201_CREATED)


class MerryDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)

        members_count = merry.members.filter(is_active=True).count()
        seats_count = merry.seats.filter(is_active=True).count()

        my_member = MerryMember.objects.filter(
            merry=merry,
            user=request.user,
            is_active=True,
        ).first()

        if my_member:
            try:
                with transaction.atomic():
                    merry_services.ensure_member_dues_up_to_current_turn(member=my_member)
            except Exception:
                pass

        my_join_request = (
            MerryJoinRequest.objects.filter(merry=merry, user=request.user)
            .order_by("-created_at", "-id")
            .first()
        )

        next_turn = None
        try:
            next_turn = merry_services.get_next_payout_turn(merry_id=merry.id)
        except Exception:
            next_turn = None

        data = {
            "id": merry.id,
            "name": merry.name,
            "contribution_amount": str(merry.contribution_amount),
            "cycle_duration_weeks": merry.cycle_duration_weeks,
            "payout_order_type": merry.payout_order_type,
            "next_payout_date": merry.next_payout_date,
            "payout_frequency": merry.payout_frequency,
            "payouts_per_period": 1,
            "is_open": getattr(merry, "is_open", True),
            "max_seats": getattr(merry, "max_seats", 0),
            "available_seats": merry.available_seats() if hasattr(merry, "available_seats") else None,
            "available_seat_numbers": (
                merry.available_seat_numbers() if hasattr(merry, "available_seat_numbers") else None
            ),
            "members_count": members_count,
            "seats_count": seats_count,
            "total_pool_per_slot": str(merry.total_pool_per_slot()),
            "total_pool_per_period": str(merry.total_pool_per_period()),
            "created_by": merry.created_by_id,
            "created_at": merry.created_at,
            "is_member": bool(my_member),
            "my_member_id": my_member.id if my_member else None,
            "my_join_request": (
                {
                    "id": my_join_request.id,
                    "status": my_join_request.status,
                    "requested_seats": my_join_request.requested_seats,
                    "created_at": my_join_request.created_at,
                    "reviewed_at": my_join_request.reviewed_at,
                }
                if my_join_request
                else None
            ),
            "can_request_join": bool(
                not my_member
                and getattr(merry, "is_open", True)
                and (
                    merry.available_seats() is None
                    or merry.available_seats() > 0
                )
            ),
            "next_turn": next_turn,
        }
        data.update(_merry_policy_dict(merry))

        return Response(data, status=status.HTTP_200_OK)


class MerryMobileDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        try:
            data = merry_services.get_merry_mobile_detail(
                user=request.user,
                merry_id=merry_id,
            )
        except Exception as e:
            raise _service_error(e)

        return Response(data, status=status.HTTP_200_OK)


class MerryMobileReadinessRowsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        try:
            data = merry_services.get_merry_mobile_readiness_rows(
                user=request.user,
                merry_id=merry_id,
            )
        except Exception as e:
            raise _service_error(e)

        return Response(data, status=status.HTTP_200_OK)


# ==========================================
# Members & Seats
# ==========================================
class MerryMembersView(APIView):
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
# Slot config (legacy compatibility only)
# ==========================================
class SlotConfigView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        if not user_can_view_merry(request.user, merry):
            raise PermissionDenied("Not allowed.")

        rows = MerrySlotConfig.objects.filter(merry=merry).order_by("slot_no")
        return Response(
            {
                "legacy": True,
                "message": "Slot configuration is legacy in queue-based ROSCA.",
                "rows": [
                    {"slot_no": r.slot_no, "weekday": r.weekday, "weekday_name": r.get_weekday_display()}
                    for r in rows
                ],
            },
            status=status.HTTP_200_OK,
        )

    @transaction.atomic
    def post(self, request, merry_id: int):
        raise ValidationError("Slot configuration is no longer used in the active ROSCA flow.")


# ==========================================
# Join requests flow
# ==========================================
class RequestToJoinMerryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, merry_id: int):
        try:
            jr = merry_services.request_to_join_merry(
                user=request.user,
                merry_id=merry_id,
                note=(request.data.get("note") or "").strip(),
                requested_seats=request.data.get("requested_seats", 1),
            )
        except Exception as e:
            raise _service_error(e)

        status_code = status.HTTP_201_CREATED if jr.status == "PENDING" else status.HTTP_200_OK
        return Response(
            {
                "message": "Join request submitted.",
                "request_id": jr.id,
                "status": jr.status,
                "requested_seats": jr.requested_seats,
            },
            status=status_code,
        )


class CancelJoinRequestView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, request_id: int):
        try:
            merry_services.cancel_join_request(user=request.user, request_id=request_id)
        except Exception as e:
            raise _service_error(e)
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

        assigned_seat_numbers = request.data.get("assigned_seat_numbers", None)

        if assigned_seat_numbers is not None:
            if not isinstance(assigned_seat_numbers, list):
                raise ValidationError("assigned_seat_numbers must be a list of integers.")
            parsed_assigned_seat_numbers = [
                parse_int(v, "assigned_seat_numbers item", min_value=1) for v in assigned_seat_numbers
            ]
        else:
            parsed_assigned_seat_numbers = None

        try:
            member, seats = merry_services.admin_approve_join_request(
                admin_user=request.user,
                request_id=request_id,
                assigned_seat_numbers=parsed_assigned_seat_numbers,
            )
        except Exception as e:
            raise _service_error(e)

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

    @transaction.atomic
    def post(self, request, request_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        note = (request.data.get("note") or "").strip()
        try:
            merry_services.admin_reject_join_request(
                admin_user=request.user,
                request_id=request_id,
                note=note,
            )
        except Exception as e:
            raise _service_error(e)

        return Response({"message": "Join request rejected."}, status=status.HTTP_200_OK)


# ==========================================
# Dashboard / summary
# ==========================================
class MyAllMerryDueSummaryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            summary = merry_services.get_user_merry_due_summary(user=request.user)
        except Exception as e:
            raise _service_error(e)
        return Response(summary, status=status.HTTP_200_OK)


class MerryPaymentBreakdownView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        include_next = parse_bool(request.query_params.get("include_next"), default=False)

        try:
            data = merry_services.get_merry_member_payment_breakdown(
                user=request.user,
                merry_id=merry_id,
                include_next=include_next,
            )
        except Exception as e:
            raise _service_error(e)

        return Response(data, status=status.HTTP_200_OK)


class MyMerryWalletView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        wallet = MerryWallet.objects.filter(user=request.user).first()

        return Response(
            {
                "user_id": request.user.id,
                "wallet_balance": str(wallet.balance if wallet else Decimal("0.00")),
                "updated_at": wallet.updated_at if wallet else None,
            },
            status=status.HTTP_200_OK,
        )


class MyMerryWalletTransactionsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = (
            MerryWalletTransaction.objects.filter(user=request.user)
            .order_by("-created_at", "-id")[:100]
        )

        data = [
            {
                "id": tx.id,
                "tx_type": tx.tx_type,
                "amount": str(tx.amount),
                "balance_before": str(tx.balance_before),
                "balance_after": str(tx.balance_after),
                "reference": tx.reference,
                "narration": tx.narration,
                "mpesa_receipt_number": tx.mpesa_receipt_number,
                "created_at": tx.created_at,
            }
            for tx in qs
        ]

        return Response(
            {
                "user_id": request.user.id,
                "count": len(data),
                "results": data,
            },
            status=status.HTTP_200_OK,
        )


class AdminUserMerryWalletView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, user_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        uid = parse_int(user_id, "user_id", min_value=1)
        wallet = MerryWallet.objects.filter(user_id=uid).first()

        txs = (
            MerryWalletTransaction.objects.filter(user_id=uid)
            .order_by("-created_at", "-id")[:50]
        )

        return Response(
            {
                "user_id": uid,
                "wallet_balance": str(wallet.balance if wallet else Decimal("0.00")),
                "updated_at": wallet.updated_at if wallet else None,
                "transactions": [
                    {
                        "id": tx.id,
                        "tx_type": tx.tx_type,
                        "amount": str(tx.amount),
                        "balance_before": str(tx.balance_before),
                        "balance_after": str(tx.balance_after),
                        "reference": tx.reference,
                        "narration": tx.narration,
                        "mpesa_receipt_number": tx.mpesa_receipt_number,
                        "created_at": tx.created_at,
                    }
                    for tx in txs
                ],
            },
            status=status.HTTP_200_OK,
        )


# ==========================================
# Dues & Payments
# ==========================================
class EnsureDuesForCurrentPeriodView(APIView):
    """
    Compatibility endpoint.
    In queue-based ROSCA, this prepares the current payout and current dues.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, merry_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        try:
            payout = merry_services.ensure_current_payout_exists(merry_id=merry_id)
            created = merry_services.ensure_dues_for_current_payout(merry_id=merry_id)
            merry = get_merry_or_404(merry_id)
            actual_period_key = payout.period_key or current_period_key(merry)
        except Exception as e:
            raise _service_error(e)

        return Response(
            {
                "message": "Current payout dues prepared.",
                "period_key": actual_period_key,
                "created": created,
                "payout_id": getattr(payout, "id", None),
                "turn_no": getattr(payout, "turn_no", None),
                "cycle_no": getattr(payout, "cycle_no", None),
                "scheduled_date": getattr(payout, "scheduled_date", None),
            },
            status=status.HTTP_200_OK,
        )


class MyMerryDuesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        member = get_member_or_404(merry_id, request.user)

        try:
            preview = merry_services.get_next_payout_turn(merry_id=merry.id)
            period_key = preview["period_key"]
        except Exception:
            period_key = current_period_key(merry)

        with transaction.atomic():
            try:
                merry_services.ensure_dues_for_member_period(merry, member, period_key)
            except Exception as e:
                raise _service_error(e)

        dues = (
            MerryContributionDue.objects.filter(
                merry=merry,
                seat__member=member,
                seat__is_active=True,
            )
            .exclude(status__in=["PAID", "CANCELLED"])
            .select_related("seat", "payout")
            .order_by("due_date", "seat__seat_no", "id")
        )

        data = [
            {
                "due_id": d.id,
                "payout_id": getattr(d, "payout_id", None),
                "turn_no": getattr(d.payout, "turn_no", None) if getattr(d, "payout", None) else None,
                "cycle_no": getattr(d.payout, "cycle_no", None) if getattr(d, "payout", None) else None,
                "scheduled_date": getattr(d.payout, "scheduled_date", None) if getattr(d, "payout", None) else None,
                "period_key": d.period_key,
                "slot_no": d.slot_no,
                "seat_id": d.seat_id,
                "seat_no": d.seat.seat_no,
                "base_amount": str(q2(getattr(d, "base_amount", Decimal("0.00")) or Decimal("0.00"))),
                "penalty_amount": str(q2(getattr(d, "penalty_amount", Decimal("0.00")) or Decimal("0.00"))),
                "due_amount": str(d.due_amount),
                "paid_amount": str(d.paid_amount),
                "status": d.status,
                "outstanding": str(d.outstanding()),
                "due_date": d.due_date,
                "days_overdue": int(getattr(d, "days_overdue", 0) or 0),
                "is_advance_payable": getattr(d, "is_advance_payable", False),
                "updated_at": d.updated_at,
            }
            for d in dues
        ]

        return Response(
            {
                "merry_id": merry.id,
                "period_key": period_key,
                "payouts_per_period": 1,
                "data": data,
            },
            status=status.HTTP_200_OK,
        )


class AdminDuesView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")
        merry = get_merry_or_404(merry_id)

        try:
            preview = merry_services.get_next_payout_turn(merry_id=merry.id)
            period_key = preview["period_key"]
        except Exception:
            period_key = current_period_key(merry)

        try:
            merry_services.ensure_current_payout_exists(merry_id=merry.id)
            merry_services.ensure_dues_for_current_payout(merry_id=merry.id)
        except Exception as e:
            raise _service_error(e)

        qs = MerryContributionDue.objects.filter(
            merry=merry,
        ).exclude(
            status="CANCELLED"
        ).select_related(
            "seat", "seat__member", "seat__member__user", "payout"
        ).order_by("due_date", "payout__turn_no", "seat__member__user_id", "seat__seat_no", "id")

        data = []
        for d in qs:
            u = d.seat.member.user
            data.append(
                {
                    "due_id": d.id,
                    "payout_id": getattr(d, "payout_id", None),
                    "turn_no": getattr(d.payout, "turn_no", None) if getattr(d, "payout", None) else None,
                    "cycle_no": getattr(d.payout, "cycle_no", None) if getattr(d, "payout", None) else None,
                    "scheduled_date": getattr(d.payout, "scheduled_date", None) if getattr(d, "payout", None) else None,
                    "period_key": d.period_key,
                    "slot_no": d.slot_no,
                    "seat_id": d.seat_id,
                    "seat_no": d.seat.seat_no,
                    "member_id": d.seat.member_id,
                    "user_id": u.id,
                    "username": getattr(u, "username", None),
                    "phone": getattr(u, "phone", None),
                    "base_amount": str(q2(getattr(d, "base_amount", Decimal("0.00")) or Decimal("0.00"))),
                    "penalty_amount": str(q2(getattr(d, "penalty_amount", Decimal("0.00")) or Decimal("0.00"))),
                    "due_amount": str(d.due_amount),
                    "paid_amount": str(d.paid_amount),
                    "status": d.status,
                    "outstanding": str(d.outstanding()),
                    "due_date": d.due_date,
                    "days_overdue": int(getattr(d, "days_overdue", 0) or 0),
                    "is_advance_payable": getattr(d, "is_advance_payable", False),
                    "updated_at": d.updated_at,
                }
            )

        totals = qs.aggregate(total_due=Sum("due_amount"), total_paid=Sum("paid_amount"))

        return Response(
            {
                "merry_id": merry.id,
                "period_key": period_key,
                "slot_no": 1,
                "total_due": str(q2(totals.get("total_due") or Decimal("0"))),
                "total_paid_allocated": str(q2(totals.get("total_paid") or Decimal("0"))),
                "rows": data,
            },
            status=status.HTTP_200_OK,
        )


class CreatePaymentIntentView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, merry_id: int):
        payer_phone = (request.data.get("payer_phone") or getattr(request.user, "phone", "") or "").strip()

        try:
            pay = merry_services.create_payment_intent(
                user=request.user,
                merry_id=merry_id,
                amount=request.data.get("amount"),
                payer_phone=payer_phone,
                gross_amount=request.data.get("gross_amount"),
                transaction_fee=request.data.get("transaction_fee", "0.00"),
            )
        except Exception as e:
            raise _service_error(e)

        return Response(
            {
                "message": "Payment intent created.",
                "payment_id": pay.id,
                "merry_id": pay.merry_id,
                "beneficiary_member_id": pay.beneficiary_member_id,
                "amount": str(pay.amount),
                "gross_amount": str(getattr(pay, "gross_amount", pay.amount)),
                "transaction_fee": str(getattr(pay, "transaction_fee", "0.00")),
                "payer_phone": pay.payer_phone,
                "period_key": pay.period_key,
                "status": pay.status,
            },
            status=status.HTTP_201_CREATED,
        )


class MyPaymentsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = merry_services.list_my_payments(user=request.user, limit=200)

        data = [
            {
                "id": p.id,
                "merry_id": p.merry_id,
                "merry_name": p.merry.name,
                "beneficiary_member_id": p.beneficiary_member_id,
                "amount": str(p.amount),
                "gross_amount": str(getattr(p, "gross_amount", p.amount)),
                "transaction_fee": str(getattr(p, "transaction_fee", "0.00")),
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
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, payment_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        receipt = (request.data.get("mpesa_receipt_number") or "").strip()[:64] or None

        try:
            merry_services.confirm_payment_and_allocate(
                payment_id=payment_id,
                mpesa_receipt_number=receipt,
                paid_at=timezone.now(),
            )
        except Exception as e:
            raise _service_error(e)

        return Response({"message": "Payment confirmed and allocated."}, status=status.HTTP_200_OK)


# ==========================================
# Payout schedule + records (seat-based)
# ==========================================
class MerryPayoutScheduleView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        if not user_can_view_merry(request.user, merry):
            raise PermissionDenied("Not allowed.")

        try:
            preview = merry_services.get_next_payout_turn(merry_id=merry.id)
            period_key = preview["period_key"]
        except Exception:
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

        next_turn = None
        try:
            next_turn = merry_services.get_next_payout_turn(merry_id=merry.id)
        except Exception:
            next_turn = None

        readiness = None
        try:
            readiness = merry_services.get_payout_readiness_status(merry_id=merry.id)
        except Exception:
            readiness = None

        merry_block = {
            "id": merry.id,
            "name": merry.name,
            "payout_order_type": merry.payout_order_type,
            "contribution_amount": str(merry.contribution_amount),
            "members_count": merry.members.filter(is_active=True).count(),
            "seats_count": merry.seats.filter(is_active=True).count(),
            "payout_frequency": merry.payout_frequency,
            "payouts_per_period": 1,
            "is_open": getattr(merry, "is_open", True),
            "max_seats": getattr(merry, "max_seats", 0),
            "available_seats": merry.available_seats() if hasattr(merry, "available_seats") else None,
            "next_payout_date": merry.next_payout_date,
        }
        merry_block.update(_merry_policy_dict(merry))

        return Response(
            {
                "merry": merry_block,
                "current_period_key": period_key,
                "used_slots_in_period": used_slots,
                "next_turn": next_turn,
                "readiness": readiness,
                "seats": seats,
            },
            status=status.HTTP_200_OK,
        )


class NextPayoutTurnView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        if not user_can_view_merry(request.user, merry):
            raise PermissionDenied("Not allowed.")

        try:
            data = merry_services.get_next_payout_turn(merry_id=merry_id)
        except Exception as e:
            raise _service_error(e)

        return Response(data, status=status.HTTP_200_OK)


class PayoutReadinessView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        merry = get_merry_or_404(merry_id)
        if not user_can_view_merry(request.user, merry):
            raise PermissionDenied("Not allowed.")

        try:
            data = merry_services.get_payout_readiness_status(
                merry_id=merry_id,
                period_key=None,
                slot_no=1,
            )
        except Exception as e:
            raise _service_error(e)

        return Response(data, status=status.HTTP_200_OK)


class CreatePayoutView(APIView):
    """
    Compatibility-safe payout creation.
    Queue-based ROSCA uses one current payout event at a time.
    """
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, merry_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        compute_amount = parse_bool(request.data.get("compute_amount"), default=False)
        auto_select_next_turn = parse_bool(request.data.get("auto_select_next_turn"), default=False)
        notes = (request.data.get("notes") or "")[:255]

        merry = get_merry_or_404(merry_id)

        if auto_select_next_turn:
            try:
                preview = merry_services.get_next_payout_turn(merry_id=merry_id)
            except Exception as e:
                raise _service_error(e)

            resolved_period_key = preview["period_key"]
            resolved_slot_no = 1
            resolved_seat_id = preview["seat_id"]
        else:
            if not request.data.get("seat_id"):
                raise ValidationError("seat_id is required unless auto_select_next_turn=true.")
            resolved_seat_id = parse_int(request.data.get("seat_id"), "seat_id", min_value=1)
            resolved_period_key = (request.data.get("period_key") or "").strip() or current_period_key(merry)
            resolved_slot_no = 1

        if compute_amount:
            try:
                amount = merry_services.compute_payout_amount_for_slot(
                    merry_id=merry_id,
                    period_key=resolved_period_key,
                    slot_no=1,
                )
            except Exception as e:
                raise _service_error(e)

            if amount <= 0:
                raise ValidationError("No funds allocated for the current payout yet. Cannot compute payout amount.")
        else:
            if auto_select_next_turn and (request.data.get("amount") in [None, ""]):
                amount = None
            else:
                amount = parse_decimal(request.data.get("amount"), "amount")

        try:
            payout = merry_services.create_payout_record(
                admin_user=request.user,
                merry_id=merry_id,
                seat_id=resolved_seat_id if not auto_select_next_turn else None,
                amount=amount,
                period_key=resolved_period_key if not auto_select_next_turn else None,
                slot_no=1,
                notes=notes,
                auto_select_next_turn=auto_select_next_turn,
            )
        except Exception as e:
            raise _service_error(e)

        refreshed_next_turn = None
        try:
            refreshed_next_turn = merry_services.get_next_payout_turn(merry_id=merry_id)
        except Exception:
            refreshed_next_turn = None

        return Response(
            {
                "message": "Payout record created.",
                "payout_id": payout.id,
                "status": payout.status,
                "merry_id": payout.merry_id,
                "seat_id": payout.seat_id,
                "member_id": payout.seat.member_id,
                "user_id": payout.seat.member.user_id,
                "amount": str(payout.amount),
                "period_key": payout.period_key,
                "slot_no": payout.slot_no,
                "turn_no": getattr(payout, "turn_no", None),
                "cycle_no": getattr(payout, "cycle_no", None),
                "scheduled_date": getattr(payout, "scheduled_date", None),
                "next_turn": refreshed_next_turn,
            },
            status=status.HTTP_201_CREATED,
        )


class CreateNextPayoutView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, merry_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        notes = (request.data.get("notes") or "")[:255]

        try:
            payout = merry_services.create_next_cycle_payout_record(
                admin_user=request.user,
                merry_id=merry_id,
                notes=notes,
            )
        except Exception as e:
            raise _service_error(e)

        next_turn = None
        try:
            next_turn = merry_services.get_next_payout_turn(merry_id=merry_id)
        except Exception:
            next_turn = None

        return Response(
            {
                "message": "Next payout record created.",
                "payout_id": payout.id,
                "status": payout.status,
                "merry_id": payout.merry_id,
                "seat_id": payout.seat_id,
                "member_id": payout.seat.member_id,
                "user_id": payout.seat.member.user_id,
                "amount": str(payout.amount),
                "period_key": payout.period_key,
                "slot_no": payout.slot_no,
                "turn_no": getattr(payout, "turn_no", None),
                "cycle_no": getattr(payout, "cycle_no", None),
                "scheduled_date": getattr(payout, "scheduled_date", None),
                "next_turn": next_turn,
            },
            status=status.HTTP_201_CREATED,
        )


class MarkPayoutPaidView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, payout_id: int):
        if not is_admin(request.user):
            raise PermissionDenied("Admin only.")

        try:
            payout = merry_services.mark_payout_paid(
                payout_id=payout_id,
                paid_at=timezone.now(),
            )
        except Exception as e:
            raise _service_error(e)

        next_turn = None
        try:
            next_turn = merry_services.get_next_payout_turn(merry_id=payout.merry_id)
        except Exception:
            next_turn = None

        return Response(
            {
                "message": "Payout marked PAID.",
                "payout_id": payout.id,
                "merry_id": payout.merry_id,
                "turn_no": getattr(payout, "turn_no", None),
                "cycle_no": getattr(payout, "cycle_no", None),
                "scheduled_date": getattr(payout, "scheduled_date", None),
                "next_turn": next_turn,
            },
            status=status.HTTP_200_OK,
        )


class MerryMemberDashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, merry_id: int):
        try:
            data = merry_services.get_member_merry_dashboard(
                user=request.user,
                merry_id=merry_id,
            )
        except Exception as e:
            raise _service_error(e)

        return Response(data, status=status.HTTP_200_OK)

# # merry/views.py
# # ROSCA compatibility version
# # ---------------------------------------------------------
# # - Keeps most endpoints and response shapes stable
# # - Aligns flow with queue-based ROSCA (one current payout at a time)
# # - Preserves wallet, payments, join requests, dashboard, payouts
# # - Keeps some legacy compatibility endpoints but narrows them to slot_no=1
# # - Exposes penalty policy and turn-linked payout/due fields
# # ---------------------------------------------------------

# from __future__ import annotations

# from decimal import Decimal, InvalidOperation
# from typing import Optional

# from django.db import transaction
# from django.db.models import Sum
# from django.utils import timezone

# from rest_framework import permissions, status
# from rest_framework.exceptions import PermissionDenied, ValidationError
# from rest_framework.response import Response
# from rest_framework.views import APIView

# from .models import (
#     MerryGoRound,
#     MerryMember,
#     MerrySeat,
#     MerryJoinRequest,
#     MerrySlotConfig,
#     MerryContributionDue,
#     MerryPayment,
#     MerryPaymentAllocation,
#     MerryPayout,
#     MerryWallet,
#     MerryWalletTransaction,
# )
# from . import services as merry_services


# # ==========================================
# # Helpers
# # ==========================================
# def q2(x: Decimal) -> Decimal:
#     return Decimal(str(x or "0")).quantize(Decimal("0.01"))


# def parse_decimal(value, field_name: str) -> Decimal:
#     if value is None or value == "":
#         raise ValidationError(f"{field_name} is required.")
#     try:
#         return q2(Decimal(str(value)))
#     except (InvalidOperation, ValueError, TypeError):
#         raise ValidationError(f"{field_name} must be a valid number.")


# def parse_int(value, field_name: str, *, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
#     try:
#         n = int(value)
#     except (ValueError, TypeError):
#         raise ValidationError(f"{field_name} must be an integer.")

#     if min_value is not None and n < min_value:
#         raise ValidationError(f"{field_name} must be >= {min_value}.")
#     if max_value is not None and n > max_value:
#         raise ValidationError(f"{field_name} must be <= {max_value}.")
#     return n


# def parse_bool(value, default: bool = False) -> bool:
#     if value is None:
#         return default
#     if isinstance(value, bool):
#         return value
#     if isinstance(value, (int, float)):
#         return bool(value)
#     s = str(value).strip().lower()
#     if s in ("true", "1", "yes", "on"):
#         return True
#     if s in ("false", "0", "no", "off"):
#         return False
#     return default


# def is_admin(user) -> bool:
#     return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


# def get_merry_or_404(merry_id: int) -> MerryGoRound:
#     merry = MerryGoRound.objects.filter(id=merry_id).select_related("created_by").first()
#     if not merry:
#         raise ValidationError("Merry not found.")
#     return merry


# def get_member_or_404(merry_id: int, user) -> MerryMember:
#     member = (
#         MerryMember.objects.filter(merry_id=merry_id, user=user, is_active=True)
#         .select_related("merry", "user")
#         .first()
#     )
#     if not member:
#         raise ValidationError("You are not an active member of this merry.")
#     return member


# def current_period_key(merry: MerryGoRound) -> str:
#     return merry.current_period_key()


# def payouts_per_period(merry: MerryGoRound) -> int:
#     if hasattr(merry, "effective_payouts_per_period"):
#         return max(1, int(merry.effective_payouts_per_period() or 1))
#     return 1


# def validate_slot(merry: MerryGoRound, slot_no: int) -> None:
#     limit = payouts_per_period(merry)
#     if slot_no < 1 or slot_no > limit:
#         raise ValidationError(f"slot_no must be between 1 and {limit}.")


# def next_available_slot(merry: MerryGoRound, period_key: str) -> int:
#     return 1


# def user_can_view_merry(user, merry: MerryGoRound) -> bool:
#     if is_admin(user):
#         return True
#     return MerryMember.objects.filter(merry=merry, user=user, is_active=True).exists()


# def _service_error(e: Exception) -> ValidationError:
#     return ValidationError(str(e))


# def _merry_policy_dict(merry: MerryGoRound) -> dict:
#     return {
#         "penalty_mode": getattr(merry, "penalty_mode", "NONE"),
#         "flat_penalty_amount": str(q2(getattr(merry, "flat_penalty_amount", Decimal("0.00")) or Decimal("0.00"))),
#         "daily_penalty_amount": str(q2(getattr(merry, "daily_penalty_amount", Decimal("0.00")) or Decimal("0.00"))),
#         "penalty_grace_days": int(getattr(merry, "penalty_grace_days", 0) or 0),
#         "penalty_cap_amount": (
#             str(q2(getattr(merry, "penalty_cap_amount")))
#             if getattr(merry, "penalty_cap_amount", None) is not None
#             else None
#         ),
#     }


# # ==========================================
# # Allocation engine (backward-compatible wrapper)
# # ==========================================
# @transaction.atomic
# def _ensure_dues_for_member_period(merry: MerryGoRound, member: MerryMember, period_key: str) -> None:
#     try:
#         merry_services.ensure_dues_for_member_period(merry, member, period_key)
#     except Exception as e:
#         raise ValidationError(str(e))


# @transaction.atomic
# def allocate_payment(payment_id: int) -> MerryPayment:
#     try:
#         return merry_services.allocate_payment(payment_id=payment_id)
#     except Exception as e:
#         raise ValidationError(str(e))


# # ==========================================
# # Merry list / create / detail
# # ==========================================
# class AvailableMerriesView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request):
#         member_merry_ids = list(
#             MerryMember.objects.filter(user=request.user, is_active=True).values_list("merry_id", flat=True)
#         )

#         latest_join_requests = {}
#         for r in MerryJoinRequest.objects.filter(user=request.user).order_by("-created_at", "-id"):
#             if r.merry_id not in latest_join_requests:
#                 latest_join_requests[r.merry_id] = r

#         qs = MerryGoRound.objects.exclude(id__in=member_merry_ids).order_by("-id")

#         data = []
#         for m in qs:
#             jr = latest_join_requests.get(m.id)
#             available_seats = m.available_seats() if hasattr(m, "available_seats") else None
#             available_seat_numbers = (
#                 m.available_seat_numbers() if hasattr(m, "available_seat_numbers") else None
#             )

#             row = {
#                 "id": m.id,
#                 "name": m.name,
#                 "contribution_amount": str(m.contribution_amount),
#                 "cycle_duration_weeks": m.cycle_duration_weeks,
#                 "payout_order_type": m.payout_order_type,
#                 "next_payout_date": m.next_payout_date,
#                 "payout_frequency": m.payout_frequency,
#                 "payouts_per_period": 1,
#                 "is_open": getattr(m, "is_open", True),
#                 "max_seats": getattr(m, "max_seats", 0),
#                 "available_seats": available_seats,
#                 "available_seat_numbers": available_seat_numbers,
#                 "members_count": m.members.filter(is_active=True).count(),
#                 "seats_count": m.seats.filter(is_active=True).count(),
#                 "can_request_join": bool(
#                     getattr(m, "is_open", True) and (available_seats is None or available_seats > 0)
#                 ),
#                 "my_join_request": (
#                     {
#                         "id": jr.id,
#                         "status": jr.status,
#                         "requested_seats": jr.requested_seats,
#                         "created_at": jr.created_at,
#                         "reviewed_at": jr.reviewed_at,
#                     }
#                     if jr
#                     else None
#                 ),
#                 "created_at": m.created_at,
#             }
#             row.update(_merry_policy_dict(m))
#             data.append(row)

#         return Response(data, status=status.HTTP_200_OK)


# class MyMerriesView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request):
#         created = MerryGoRound.objects.filter(created_by=request.user).order_by("-id")

#         memberships = (
#             MerryMember.objects.filter(user=request.user, is_active=True)
#             .select_related("merry")
#             .order_by("-id")
#         )

#         created_data = []
#         for m in created:
#             row = {
#                 "id": m.id,
#                 "name": m.name,
#                 "contribution_amount": str(m.contribution_amount),
#                 "cycle_duration_weeks": m.cycle_duration_weeks,
#                 "payout_order_type": m.payout_order_type,
#                 "next_payout_date": m.next_payout_date,
#                 "payout_frequency": m.payout_frequency,
#                 "payouts_per_period": 1,
#                 "is_open": getattr(m, "is_open", True),
#                 "max_seats": getattr(m, "max_seats", 0),
#                 "available_seats": m.available_seats() if hasattr(m, "available_seats") else None,
#                 "members_count": m.members.filter(is_active=True).count(),
#                 "seats_count": m.seats.filter(is_active=True).count(),
#                 "created_at": m.created_at,
#             }
#             row.update(_merry_policy_dict(m))
#             created_data.append(row)

#         member_data = []
#         for mm in memberships:
#             seats_count = mm.seats.filter(is_active=True).count()
#             row = {
#                 "merry_id": mm.merry_id,
#                 "name": mm.merry.name,
#                 "contribution_amount": str(mm.merry.contribution_amount),
#                 "cycle_duration_weeks": mm.merry.cycle_duration_weeks,
#                 "payout_order_type": mm.merry.payout_order_type,
#                 "next_payout_date": mm.merry.next_payout_date,
#                 "payout_frequency": mm.merry.payout_frequency,
#                 "payouts_per_period": 1,
#                 "is_open": getattr(mm.merry, "is_open", True),
#                 "max_seats": getattr(mm.merry, "max_seats", 0),
#                 "available_seats": mm.merry.available_seats() if hasattr(mm.merry, "available_seats") else None,
#                 "joined_at": mm.joined_at,
#                 "seats_count": seats_count,
#             }
#             row.update(_merry_policy_dict(mm.merry))
#             member_data.append(row)

#         return Response({"created": created_data, "memberships": member_data}, status=status.HTTP_200_OK)


# class CreateMerryView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def post(self, request):
#         if not is_admin(request.user):
#             raise PermissionDenied("Admin only.")

#         try:
#             merry = merry_services.create_merry(
#                 creator=request.user,
#                 name=(request.data.get("name") or "").strip(),
#                 contribution_amount=request.data.get("contribution_amount"),
#                 cycle_duration_weeks=request.data.get("cycle_duration_weeks", 1),
#                 payout_order_type=request.data.get("payout_order_type", "manual"),
#                 next_payout_date=request.data.get("next_payout_date") or None,
#                 payout_frequency=request.data.get("payout_frequency", "WEEKLY"),
#                 payouts_per_period=1,
#                 is_open=request.data.get("is_open"),
#                 max_seats=request.data.get("max_seats", 0),
#                 penalty_mode=request.data.get("penalty_mode", "NONE"),
#                 flat_penalty_amount=request.data.get("flat_penalty_amount", "0.00"),
#                 daily_penalty_amount=request.data.get("daily_penalty_amount", "0.00"),
#                 penalty_grace_days=request.data.get("penalty_grace_days", 0),
#                 penalty_cap_amount=request.data.get("penalty_cap_amount", None),
#             )
#         except Exception as e:
#             raise _service_error(e)

#         data = {
#             "id": merry.id,
#             "name": merry.name,
#             "contribution_amount": str(merry.contribution_amount),
#             "cycle_duration_weeks": merry.cycle_duration_weeks,
#             "payout_order_type": merry.payout_order_type,
#             "next_payout_date": merry.next_payout_date,
#             "payout_frequency": merry.payout_frequency,
#             "payouts_per_period": 1,
#             "is_open": merry.is_open,
#             "max_seats": merry.max_seats,
#             "available_seats": merry.available_seats() if hasattr(merry, "available_seats") else None,
#             "created_at": merry.created_at,
#         }
#         data.update(_merry_policy_dict(merry))

#         return Response(data, status=status.HTTP_201_CREATED)


# class MerryDetailView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         merry = get_merry_or_404(merry_id)

#         members_count = merry.members.filter(is_active=True).count()
#         seats_count = merry.seats.filter(is_active=True).count()

#         my_member = MerryMember.objects.filter(
#             merry=merry,
#             user=request.user,
#             is_active=True,
#         ).first()

#         my_join_request = (
#             MerryJoinRequest.objects.filter(merry=merry, user=request.user)
#             .order_by("-created_at", "-id")
#             .first()
#         )

#         next_turn = None
#         try:
#             next_turn = merry_services.get_next_payout_turn(merry_id=merry.id)
#         except Exception:
#             next_turn = None

#         data = {
#             "id": merry.id,
#             "name": merry.name,
#             "contribution_amount": str(merry.contribution_amount),
#             "cycle_duration_weeks": merry.cycle_duration_weeks,
#             "payout_order_type": merry.payout_order_type,
#             "next_payout_date": merry.next_payout_date,
#             "payout_frequency": merry.payout_frequency,
#             "payouts_per_period": 1,
#             "is_open": getattr(merry, "is_open", True),
#             "max_seats": getattr(merry, "max_seats", 0),
#             "available_seats": merry.available_seats() if hasattr(merry, "available_seats") else None,
#             "available_seat_numbers": (
#                 merry.available_seat_numbers() if hasattr(merry, "available_seat_numbers") else None
#             ),
#             "members_count": members_count,
#             "seats_count": seats_count,
#             "total_pool_per_slot": str(merry.total_pool_per_slot()),
#             "total_pool_per_period": str(merry.total_pool_per_period()),
#             "created_by": merry.created_by_id,
#             "created_at": merry.created_at,
#             "is_member": bool(my_member),
#             "my_member_id": my_member.id if my_member else None,
#             "my_join_request": (
#                 {
#                     "id": my_join_request.id,
#                     "status": my_join_request.status,
#                     "requested_seats": my_join_request.requested_seats,
#                     "created_at": my_join_request.created_at,
#                     "reviewed_at": my_join_request.reviewed_at,
#                 }
#                 if my_join_request
#                 else None
#             ),
#             "can_request_join": bool(
#                 not my_member
#                 and getattr(merry, "is_open", True)
#                 and (
#                     merry.available_seats() is None
#                     or merry.available_seats() > 0
#                 )
#             ),
#             "next_turn": next_turn,
#         }
#         data.update(_merry_policy_dict(merry))

#         return Response(data, status=status.HTTP_200_OK)


# class MerryMobileDetailView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         try:
#             data = merry_services.get_merry_mobile_detail(
#                 user=request.user,
#                 merry_id=merry_id,
#             )
#         except Exception as e:
#             raise _service_error(e)

#         return Response(data, status=status.HTTP_200_OK)


# class MerryMobileReadinessRowsView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         try:
#             data = merry_services.get_merry_mobile_readiness_rows(
#                 user=request.user,
#                 merry_id=merry_id,
#             )
#         except Exception as e:
#             raise _service_error(e)

#         return Response(data, status=status.HTTP_200_OK)


# # ==========================================
# # Members & Seats
# # ==========================================
# class MerryMembersView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         merry = get_merry_or_404(merry_id)
#         if not user_can_view_merry(request.user, merry):
#             raise PermissionDenied("Not allowed.")

#         qs = MerryMember.objects.filter(merry=merry, is_active=True).select_related("user").order_by("id")
#         data = []
#         for m in qs:
#             data.append(
#                 {
#                     "member_id": m.id,
#                     "user_id": m.user_id,
#                     "username": getattr(m.user, "username", None),
#                     "phone": getattr(m.user, "phone", None),
#                     "joined_at": m.joined_at,
#                     "seats_count": m.seats.filter(is_active=True).count(),
#                 }
#             )
#         return Response(data, status=status.HTTP_200_OK)


# class MerrySeatsView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         merry = get_merry_or_404(merry_id)
#         if not user_can_view_merry(request.user, merry):
#             raise PermissionDenied("Not allowed.")

#         qs = MerrySeat.objects.filter(merry=merry, is_active=True).select_related("member", "member__user")

#         if merry.payout_order_type == "manual":
#             qs = qs.order_by("payout_position", "id")
#         else:
#             qs = qs.order_by("id")

#         data = [
#             {
#                 "seat_id": s.id,
#                 "member_id": s.member_id,
#                 "user_id": s.member.user_id,
#                 "username": getattr(s.member.user, "username", None),
#                 "phone": getattr(s.member.user, "phone", None),
#                 "seat_no": s.seat_no,
#                 "payout_position": s.payout_position,
#                 "created_at": s.created_at,
#             }
#             for s in qs
#         ]
#         return Response(data, status=status.HTTP_200_OK)


# # ==========================================
# # Slot config (legacy compatibility only)
# # ==========================================
# class SlotConfigView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         merry = get_merry_or_404(merry_id)
#         if not user_can_view_merry(request.user, merry):
#             raise PermissionDenied("Not allowed.")

#         rows = MerrySlotConfig.objects.filter(merry=merry).order_by("slot_no")
#         return Response(
#             {
#                 "legacy": True,
#                 "message": "Slot configuration is legacy in queue-based ROSCA.",
#                 "rows": [
#                     {"slot_no": r.slot_no, "weekday": r.weekday, "weekday_name": r.get_weekday_display()}
#                     for r in rows
#                 ],
#             },
#             status=status.HTTP_200_OK,
#         )

#     @transaction.atomic
#     def post(self, request, merry_id: int):
#         raise ValidationError("Slot configuration is no longer used in the active ROSCA flow.")


# # ==========================================
# # Join requests flow
# # ==========================================
# class RequestToJoinMerryView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     @transaction.atomic
#     def post(self, request, merry_id: int):
#         try:
#             jr = merry_services.request_to_join_merry(
#                 user=request.user,
#                 merry_id=merry_id,
#                 note=(request.data.get("note") or "").strip(),
#                 requested_seats=request.data.get("requested_seats", 1),
#             )
#         except Exception as e:
#             raise _service_error(e)

#         status_code = status.HTTP_201_CREATED if jr.status == "PENDING" else status.HTTP_200_OK
#         return Response(
#             {
#                 "message": "Join request submitted.",
#                 "request_id": jr.id,
#                 "status": jr.status,
#                 "requested_seats": jr.requested_seats,
#             },
#             status=status_code,
#         )


# class CancelJoinRequestView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     @transaction.atomic
#     def post(self, request, request_id: int):
#         try:
#             merry_services.cancel_join_request(user=request.user, request_id=request_id)
#         except Exception as e:
#             raise _service_error(e)
#         return Response({"message": "Join request cancelled."}, status=status.HTTP_200_OK)


# class MyJoinRequestsView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request):
#         qs = MerryJoinRequest.objects.filter(user=request.user).select_related("merry").order_by("-created_at", "-id")
#         data = [
#             {
#                 "id": r.id,
#                 "merry_id": r.merry_id,
#                 "merry_name": r.merry.name,
#                 "status": r.status,
#                 "note": r.note,
#                 "requested_seats": r.requested_seats,
#                 "created_at": r.created_at,
#                 "reviewed_at": r.reviewed_at,
#             }
#             for r in qs
#         ]
#         return Response(data, status=status.HTTP_200_OK)


# class AdminListJoinRequestsView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         if not is_admin(request.user):
#             raise PermissionDenied("Admin only.")
#         merry = get_merry_or_404(merry_id)

#         status_filter = (request.query_params.get("status") or "").strip().upper()
#         qs = MerryJoinRequest.objects.filter(merry=merry).select_related("user").order_by("-created_at", "-id")
#         if status_filter:
#             qs = qs.filter(status=status_filter)

#         data = [
#             {
#                 "id": r.id,
#                 "user_id": r.user_id,
#                 "username": getattr(r.user, "username", None),
#                 "phone": getattr(r.user, "phone", None),
#                 "status": r.status,
#                 "note": r.note,
#                 "requested_seats": r.requested_seats,
#                 "created_at": r.created_at,
#                 "reviewed_at": r.reviewed_at,
#             }
#             for r in qs
#         ]
#         return Response(data, status=status.HTTP_200_OK)


# class AdminApproveJoinRequestView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     @transaction.atomic
#     def post(self, request, request_id: int):
#         if not is_admin(request.user):
#             raise PermissionDenied("Admin only.")

#         assigned_seat_numbers = request.data.get("assigned_seat_numbers", None)

#         if assigned_seat_numbers is not None:
#             if not isinstance(assigned_seat_numbers, list):
#                 raise ValidationError("assigned_seat_numbers must be a list of integers.")
#             parsed_assigned_seat_numbers = [
#                 parse_int(v, "assigned_seat_numbers item", min_value=1) for v in assigned_seat_numbers
#             ]
#         else:
#             parsed_assigned_seat_numbers = None

#         try:
#             member, seats = merry_services.admin_approve_join_request(
#                 admin_user=request.user,
#                 request_id=request_id,
#                 assigned_seat_numbers=parsed_assigned_seat_numbers,
#             )
#         except Exception as e:
#             raise _service_error(e)

#         return Response(
#             {
#                 "message": "Join request approved.",
#                 "member_id": member.id,
#                 "merry_id": member.merry_id,
#                 "user_id": member.user_id,
#                 "seats_created": [
#                     {"seat_id": s.id, "seat_no": s.seat_no, "payout_position": s.payout_position}
#                     for s in seats
#                 ],
#             },
#             status=status.HTTP_200_OK,
#         )


# class AdminRejectJoinRequestView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     @transaction.atomic
#     def post(self, request, request_id: int):
#         if not is_admin(request.user):
#             raise PermissionDenied("Admin only.")

#         note = (request.data.get("note") or "").strip()
#         try:
#             merry_services.admin_reject_join_request(
#                 admin_user=request.user,
#                 request_id=request_id,
#                 note=note,
#             )
#         except Exception as e:
#             raise _service_error(e)

#         return Response({"message": "Join request rejected."}, status=status.HTTP_200_OK)


# # ==========================================
# # Dashboard / summary
# # ==========================================
# class MyAllMerryDueSummaryView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request):
#         try:
#             summary = merry_services.get_user_merry_due_summary(user=request.user)
#         except Exception as e:
#             raise _service_error(e)
#         return Response(summary, status=status.HTTP_200_OK)


# class MerryPaymentBreakdownView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         include_next = parse_bool(request.query_params.get("include_next"), default=False)

#         try:
#             data = merry_services.get_merry_member_payment_breakdown(
#                 user=request.user,
#                 merry_id=merry_id,
#                 include_next=include_next,
#             )
#         except Exception as e:
#             raise _service_error(e)

#         return Response(data, status=status.HTTP_200_OK)


# class MyMerryWalletView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request):
#         wallet = MerryWallet.objects.filter(user=request.user).first()

#         return Response(
#             {
#                 "user_id": request.user.id,
#                 "wallet_balance": str(wallet.balance if wallet else Decimal("0.00")),
#                 "updated_at": wallet.updated_at if wallet else None,
#             },
#             status=status.HTTP_200_OK,
#         )


# class MyMerryWalletTransactionsView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request):
#         qs = (
#             MerryWalletTransaction.objects.filter(user=request.user)
#             .order_by("-created_at", "-id")[:100]
#         )

#         data = [
#             {
#                 "id": tx.id,
#                 "tx_type": tx.tx_type,
#                 "amount": str(tx.amount),
#                 "balance_before": str(tx.balance_before),
#                 "balance_after": str(tx.balance_after),
#                 "reference": tx.reference,
#                 "narration": tx.narration,
#                 "mpesa_receipt_number": tx.mpesa_receipt_number,
#                 "created_at": tx.created_at,
#             }
#             for tx in qs
#         ]

#         return Response(
#             {
#                 "user_id": request.user.id,
#                 "count": len(data),
#                 "results": data,
#             },
#             status=status.HTTP_200_OK,
#         )


# class AdminUserMerryWalletView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, user_id: int):
#         if not is_admin(request.user):
#             raise PermissionDenied("Admin only.")

#         uid = parse_int(user_id, "user_id", min_value=1)
#         wallet = MerryWallet.objects.filter(user_id=uid).first()

#         txs = (
#             MerryWalletTransaction.objects.filter(user_id=uid)
#             .order_by("-created_at", "-id")[:50]
#         )

#         return Response(
#             {
#                 "user_id": uid,
#                 "wallet_balance": str(wallet.balance if wallet else Decimal("0.00")),
#                 "updated_at": wallet.updated_at if wallet else None,
#                 "transactions": [
#                     {
#                         "id": tx.id,
#                         "tx_type": tx.tx_type,
#                         "amount": str(tx.amount),
#                         "balance_before": str(tx.balance_before),
#                         "balance_after": str(tx.balance_after),
#                         "reference": tx.reference,
#                         "narration": tx.narration,
#                         "mpesa_receipt_number": tx.mpesa_receipt_number,
#                         "created_at": tx.created_at,
#                     }
#                     for tx in txs
#                 ],
#             },
#             status=status.HTTP_200_OK,
#         )


# # ==========================================
# # Dues & Payments
# # ==========================================
# class EnsureDuesForCurrentPeriodView(APIView):
#     """
#     Compatibility endpoint.
#     In queue-based ROSCA, this prepares the current payout and current dues.
#     """
#     permission_classes = [permissions.IsAuthenticated]

#     @transaction.atomic
#     def post(self, request, merry_id: int):
#         if not is_admin(request.user):
#             raise PermissionDenied("Admin only.")

#         try:
#             payout = merry_services.ensure_current_payout_exists(merry_id=merry_id)
#             created = merry_services.ensure_dues_for_current_payout(merry_id=merry_id)
#             merry = get_merry_or_404(merry_id)
#             actual_period_key = payout.period_key or current_period_key(merry)
#         except Exception as e:
#             raise _service_error(e)

#         return Response(
#             {
#                 "message": "Current payout dues prepared.",
#                 "period_key": actual_period_key,
#                 "created": created,
#                 "payout_id": getattr(payout, "id", None),
#                 "turn_no": getattr(payout, "turn_no", None),
#                 "cycle_no": getattr(payout, "cycle_no", None),
#                 "scheduled_date": getattr(payout, "scheduled_date", None),
#             },
#             status=status.HTTP_200_OK,
#         )


# class MyMerryDuesView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         merry = get_merry_or_404(merry_id)
#         member = get_member_or_404(merry_id, request.user)

#         try:
#             preview = merry_services.get_next_payout_turn(merry_id=merry.id)
#             period_key = preview["period_key"]
#         except Exception:
#             period_key = current_period_key(merry)

#         with transaction.atomic():
#             try:
#                 merry_services.ensure_dues_for_member_period(merry, member, period_key)
#             except Exception as e:
#                 raise _service_error(e)

#         dues = (
#             MerryContributionDue.objects.filter(
#                 merry=merry,
#                 seat__member=member,
#                 seat__is_active=True,
#             )
#             .exclude(status__in=["PAID", "CANCELLED"])
#             .select_related("seat", "payout")
#             .order_by("due_date", "seat__seat_no", "id")
#         )

#         data = [
#             {
#                 "due_id": d.id,
#                 "payout_id": getattr(d, "payout_id", None),
#                 "turn_no": getattr(d.payout, "turn_no", None) if getattr(d, "payout", None) else None,
#                 "cycle_no": getattr(d.payout, "cycle_no", None) if getattr(d, "payout", None) else None,
#                 "scheduled_date": getattr(d.payout, "scheduled_date", None) if getattr(d, "payout", None) else None,
#                 "period_key": d.period_key,
#                 "slot_no": d.slot_no,
#                 "seat_id": d.seat_id,
#                 "seat_no": d.seat.seat_no,
#                 "base_amount": str(q2(getattr(d, "base_amount", Decimal("0.00")) or Decimal("0.00"))),
#                 "penalty_amount": str(q2(getattr(d, "penalty_amount", Decimal("0.00")) or Decimal("0.00"))),
#                 "due_amount": str(d.due_amount),
#                 "paid_amount": str(d.paid_amount),
#                 "status": d.status,
#                 "outstanding": str(d.outstanding()),
#                 "due_date": d.due_date,
#                 "days_overdue": int(getattr(d, "days_overdue", 0) or 0),
#                 "is_advance_payable": getattr(d, "is_advance_payable", False),
#                 "updated_at": d.updated_at,
#             }
#             for d in dues
#         ]

#         return Response(
#             {
#                 "merry_id": merry.id,
#                 "period_key": period_key,
#                 "payouts_per_period": 1,
#                 "data": data,
#             },
#             status=status.HTTP_200_OK,
#         )


# class AdminDuesView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         if not is_admin(request.user):
#             raise PermissionDenied("Admin only.")
#         merry = get_merry_or_404(merry_id)

#         try:
#             preview = merry_services.get_next_payout_turn(merry_id=merry.id)
#             period_key = preview["period_key"]
#         except Exception:
#             period_key = current_period_key(merry)

#         try:
#             merry_services.ensure_current_payout_exists(merry_id=merry.id)
#             merry_services.ensure_dues_for_current_payout(merry_id=merry.id)
#         except Exception as e:
#             raise _service_error(e)

#         qs = MerryContributionDue.objects.filter(
#             merry=merry,
#         ).exclude(
#             status="CANCELLED"
#         ).select_related(
#             "seat", "seat__member", "seat__member__user", "payout"
#         ).order_by("due_date", "payout__turn_no", "seat__member__user_id", "seat__seat_no", "id")

#         data = []
#         for d in qs:
#             u = d.seat.member.user
#             data.append(
#                 {
#                     "due_id": d.id,
#                     "payout_id": getattr(d, "payout_id", None),
#                     "turn_no": getattr(d.payout, "turn_no", None) if getattr(d, "payout", None) else None,
#                     "cycle_no": getattr(d.payout, "cycle_no", None) if getattr(d, "payout", None) else None,
#                     "scheduled_date": getattr(d.payout, "scheduled_date", None) if getattr(d, "payout", None) else None,
#                     "period_key": d.period_key,
#                     "slot_no": d.slot_no,
#                     "seat_id": d.seat_id,
#                     "seat_no": d.seat.seat_no,
#                     "member_id": d.seat.member_id,
#                     "user_id": u.id,
#                     "username": getattr(u, "username", None),
#                     "phone": getattr(u, "phone", None),
#                     "base_amount": str(q2(getattr(d, "base_amount", Decimal("0.00")) or Decimal("0.00"))),
#                     "penalty_amount": str(q2(getattr(d, "penalty_amount", Decimal("0.00")) or Decimal("0.00"))),
#                     "due_amount": str(d.due_amount),
#                     "paid_amount": str(d.paid_amount),
#                     "status": d.status,
#                     "outstanding": str(d.outstanding()),
#                     "due_date": d.due_date,
#                     "days_overdue": int(getattr(d, "days_overdue", 0) or 0),
#                     "is_advance_payable": getattr(d, "is_advance_payable", False),
#                     "updated_at": d.updated_at,
#                 }
#             )

#         totals = qs.aggregate(total_due=Sum("due_amount"), total_paid=Sum("paid_amount"))

#         return Response(
#             {
#                 "merry_id": merry.id,
#                 "period_key": period_key,
#                 "slot_no": 1,
#                 "total_due": str(q2(totals.get("total_due") or Decimal("0"))),
#                 "total_paid_allocated": str(q2(totals.get("total_paid") or Decimal("0"))),
#                 "rows": data,
#             },
#             status=status.HTTP_200_OK,
#         )


# class CreatePaymentIntentView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     @transaction.atomic
#     def post(self, request, merry_id: int):
#         payer_phone = (request.data.get("payer_phone") or getattr(request.user, "phone", "") or "").strip()

#         try:
#             pay = merry_services.create_payment_intent(
#                 user=request.user,
#                 merry_id=merry_id,
#                 amount=request.data.get("amount"),
#                 payer_phone=payer_phone,
#                 gross_amount=request.data.get("gross_amount"),
#                 transaction_fee=request.data.get("transaction_fee", "0.00"),
#             )
#         except Exception as e:
#             raise _service_error(e)

#         return Response(
#             {
#                 "message": "Payment intent created.",
#                 "payment_id": pay.id,
#                 "merry_id": pay.merry_id,
#                 "beneficiary_member_id": pay.beneficiary_member_id,
#                 "amount": str(pay.amount),
#                 "gross_amount": str(getattr(pay, "gross_amount", pay.amount)),
#                 "transaction_fee": str(getattr(pay, "transaction_fee", "0.00")),
#                 "payer_phone": pay.payer_phone,
#                 "period_key": pay.period_key,
#                 "status": pay.status,
#             },
#             status=status.HTTP_201_CREATED,
#         )


# class MyPaymentsView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request):
#         qs = merry_services.list_my_payments(user=request.user, limit=200)

#         data = [
#             {
#                 "id": p.id,
#                 "merry_id": p.merry_id,
#                 "merry_name": p.merry.name,
#                 "beneficiary_member_id": p.beneficiary_member_id,
#                 "amount": str(p.amount),
#                 "gross_amount": str(getattr(p, "gross_amount", p.amount)),
#                 "transaction_fee": str(getattr(p, "transaction_fee", "0.00")),
#                 "status": p.status,
#                 "paid_at": p.paid_at,
#                 "payer_phone": p.payer_phone,
#                 "mpesa_receipt_number": p.mpesa_receipt_number,
#                 "period_key": p.period_key,
#                 "created_at": p.created_at,
#             }
#             for p in qs
#         ]
#         return Response(data, status=status.HTTP_200_OK)


# class AdminMarkPaymentConfirmedView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     @transaction.atomic
#     def post(self, request, payment_id: int):
#         if not is_admin(request.user):
#             raise PermissionDenied("Admin only.")

#         receipt = (request.data.get("mpesa_receipt_number") or "").strip()[:64] or None

#         try:
#             merry_services.confirm_payment_and_allocate(
#                 payment_id=payment_id,
#                 mpesa_receipt_number=receipt,
#                 paid_at=timezone.now(),
#             )
#         except Exception as e:
#             raise _service_error(e)

#         return Response({"message": "Payment confirmed and allocated."}, status=status.HTTP_200_OK)


# # ==========================================
# # Payout schedule + records (seat-based)
# # ==========================================
# class MerryPayoutScheduleView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         merry = get_merry_or_404(merry_id)
#         if not user_can_view_merry(request.user, merry):
#             raise PermissionDenied("Not allowed.")

#         try:
#             preview = merry_services.get_next_payout_turn(merry_id=merry.id)
#             period_key = preview["period_key"]
#         except Exception:
#             period_key = current_period_key(merry)

#         used_slots = list(
#             MerryPayout.objects.filter(merry=merry, period_key=period_key)
#             .order_by("slot_no")
#             .values_list("slot_no", flat=True)
#         )

#         seats_qs = MerrySeat.objects.filter(merry=merry, is_active=True).select_related("member", "member__user")
#         if merry.payout_order_type == "manual":
#             seats_qs = seats_qs.order_by("payout_position", "id")
#         else:
#             seats_qs = seats_qs.order_by("id")

#         seats = [
#             {
#                 "seat_id": s.id,
#                 "member_id": s.member_id,
#                 "user_id": s.member.user_id,
#                 "username": getattr(s.member.user, "username", None),
#                 "phone": getattr(s.member.user, "phone", None),
#                 "seat_no": s.seat_no,
#                 "payout_position": s.payout_position,
#             }
#             for s in seats_qs
#         ]

#         next_turn = None
#         try:
#             next_turn = merry_services.get_next_payout_turn(merry_id=merry.id)
#         except Exception:
#             next_turn = None

#         readiness = None
#         try:
#             readiness = merry_services.get_payout_readiness_status(merry_id=merry.id)
#         except Exception:
#             readiness = None

#         merry_block = {
#             "id": merry.id,
#             "name": merry.name,
#             "payout_order_type": merry.payout_order_type,
#             "contribution_amount": str(merry.contribution_amount),
#             "members_count": merry.members.filter(is_active=True).count(),
#             "seats_count": merry.seats.filter(is_active=True).count(),
#             "payout_frequency": merry.payout_frequency,
#             "payouts_per_period": 1,
#             "is_open": getattr(merry, "is_open", True),
#             "max_seats": getattr(merry, "max_seats", 0),
#             "available_seats": merry.available_seats() if hasattr(merry, "available_seats") else None,
#             "next_payout_date": merry.next_payout_date,
#         }
#         merry_block.update(_merry_policy_dict(merry))

#         return Response(
#             {
#                 "merry": merry_block,
#                 "current_period_key": period_key,
#                 "used_slots_in_period": used_slots,
#                 "next_turn": next_turn,
#                 "readiness": readiness,
#                 "seats": seats,
#             },
#             status=status.HTTP_200_OK,
#         )


# class NextPayoutTurnView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         merry = get_merry_or_404(merry_id)
#         if not user_can_view_merry(request.user, merry):
#             raise PermissionDenied("Not allowed.")

#         try:
#             data = merry_services.get_next_payout_turn(merry_id=merry_id)
#         except Exception as e:
#             raise _service_error(e)

#         return Response(data, status=status.HTTP_200_OK)


# class PayoutReadinessView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         merry = get_merry_or_404(merry_id)
#         if not user_can_view_merry(request.user, merry):
#             raise PermissionDenied("Not allowed.")

#         try:
#             data = merry_services.get_payout_readiness_status(
#                 merry_id=merry_id,
#                 period_key=None,
#                 slot_no=1,
#             )
#         except Exception as e:
#             raise _service_error(e)

#         return Response(data, status=status.HTTP_200_OK)


# class CreatePayoutView(APIView):
#     """
#     Compatibility-safe payout creation.
#     Queue-based ROSCA uses one current payout event at a time.
#     """
#     permission_classes = [permissions.IsAuthenticated]

#     @transaction.atomic
#     def post(self, request, merry_id: int):
#         if not is_admin(request.user):
#             raise PermissionDenied("Admin only.")

#         compute_amount = parse_bool(request.data.get("compute_amount"), default=False)
#         auto_select_next_turn = parse_bool(request.data.get("auto_select_next_turn"), default=False)
#         notes = (request.data.get("notes") or "")[:255]

#         merry = get_merry_or_404(merry_id)

#         if auto_select_next_turn:
#             try:
#                 preview = merry_services.get_next_payout_turn(merry_id=merry_id)
#             except Exception as e:
#                 raise _service_error(e)

#             resolved_period_key = preview["period_key"]
#             resolved_slot_no = 1
#             resolved_seat_id = preview["seat_id"]
#         else:
#             if not request.data.get("seat_id"):
#                 raise ValidationError("seat_id is required unless auto_select_next_turn=true.")
#             resolved_seat_id = parse_int(request.data.get("seat_id"), "seat_id", min_value=1)
#             resolved_period_key = (request.data.get("period_key") or "").strip() or current_period_key(merry)
#             resolved_slot_no = 1

#         if compute_amount:
#             try:
#                 amount = merry_services.compute_payout_amount_for_slot(
#                     merry_id=merry_id,
#                     period_key=resolved_period_key,
#                     slot_no=1,
#                 )
#             except Exception as e:
#                 raise _service_error(e)

#             if amount <= 0:
#                 raise ValidationError("No funds allocated for the current payout yet. Cannot compute payout amount.")
#         else:
#             if auto_select_next_turn and (request.data.get("amount") in [None, ""]):
#                 amount = None
#             else:
#                 amount = parse_decimal(request.data.get("amount"), "amount")

#         try:
#             payout = merry_services.create_payout_record(
#                 admin_user=request.user,
#                 merry_id=merry_id,
#                 seat_id=resolved_seat_id if not auto_select_next_turn else None,
#                 amount=amount,
#                 period_key=resolved_period_key if not auto_select_next_turn else None,
#                 slot_no=1,
#                 notes=notes,
#                 auto_select_next_turn=auto_select_next_turn,
#             )
#         except Exception as e:
#             raise _service_error(e)

#         refreshed_next_turn = None
#         try:
#             refreshed_next_turn = merry_services.get_next_payout_turn(merry_id=merry_id)
#         except Exception:
#             refreshed_next_turn = None

#         return Response(
#             {
#                 "message": "Payout record created.",
#                 "payout_id": payout.id,
#                 "status": payout.status,
#                 "merry_id": payout.merry_id,
#                 "seat_id": payout.seat_id,
#                 "member_id": payout.seat.member_id,
#                 "user_id": payout.seat.member.user_id,
#                 "amount": str(payout.amount),
#                 "period_key": payout.period_key,
#                 "slot_no": payout.slot_no,
#                 "turn_no": getattr(payout, "turn_no", None),
#                 "cycle_no": getattr(payout, "cycle_no", None),
#                 "scheduled_date": getattr(payout, "scheduled_date", None),
#                 "next_turn": refreshed_next_turn,
#             },
#             status=status.HTTP_201_CREATED,
#         )


# class CreateNextPayoutView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     @transaction.atomic
#     def post(self, request, merry_id: int):
#         if not is_admin(request.user):
#             raise PermissionDenied("Admin only.")

#         notes = (request.data.get("notes") or "")[:255]

#         try:
#             payout = merry_services.create_next_cycle_payout_record(
#                 admin_user=request.user,
#                 merry_id=merry_id,
#                 notes=notes,
#             )
#         except Exception as e:
#             raise _service_error(e)

#         next_turn = None
#         try:
#             next_turn = merry_services.get_next_payout_turn(merry_id=merry_id)
#         except Exception:
#             next_turn = None

#         return Response(
#             {
#                 "message": "Next payout record created.",
#                 "payout_id": payout.id,
#                 "status": payout.status,
#                 "merry_id": payout.merry_id,
#                 "seat_id": payout.seat_id,
#                 "member_id": payout.seat.member_id,
#                 "user_id": payout.seat.member.user_id,
#                 "amount": str(payout.amount),
#                 "period_key": payout.period_key,
#                 "slot_no": payout.slot_no,
#                 "turn_no": getattr(payout, "turn_no", None),
#                 "cycle_no": getattr(payout, "cycle_no", None),
#                 "scheduled_date": getattr(payout, "scheduled_date", None),
#                 "next_turn": next_turn,
#             },
#             status=status.HTTP_201_CREATED,
#         )


# class MarkPayoutPaidView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     @transaction.atomic
#     def post(self, request, payout_id: int):
#         if not is_admin(request.user):
#             raise PermissionDenied("Admin only.")

#         try:
#             payout = merry_services.mark_payout_paid(
#                 payout_id=payout_id,
#                 paid_at=timezone.now(),
#             )
#         except Exception as e:
#             raise _service_error(e)

#         next_turn = None
#         try:
#             next_turn = merry_services.get_next_payout_turn(merry_id=payout.merry_id)
#         except Exception:
#             next_turn = None

#         return Response(
#             {
#                 "message": "Payout marked PAID.",
#                 "payout_id": payout.id,
#                 "merry_id": payout.merry_id,
#                 "turn_no": getattr(payout, "turn_no", None),
#                 "cycle_no": getattr(payout, "cycle_no", None),
#                 "scheduled_date": getattr(payout, "scheduled_date", None),
#                 "next_turn": next_turn,
#             },
#             status=status.HTTP_200_OK,
#         )


# class MerryMemberDashboardView(APIView):
#     permission_classes = [permissions.IsAuthenticated]

#     def get(self, request, merry_id: int):
#         try:
#             data = merry_services.get_member_merry_dashboard(
#                 user=request.user,
#                 merry_id=merry_id,
#             )
#         except Exception as e:
#             raise _service_error(e)

#         return Response(data, status=status.HTTP_200_OK)

